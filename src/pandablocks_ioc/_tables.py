# IOC Table record support

import logging
import typing
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Union

import numpy as np
import numpy.typing as npt
from epicsdbbuilder import RecordName
from epicsdbbuilder.recordbase import PP
from pandablocks.asyncio import AsyncioClient
from pandablocks.commands import GetMultiline, Put
from pandablocks.responses import TableFieldDetails, TableFieldInfo
from pandablocks.utils import table_to_words, words_to_table
from pvi.device import ComboBox, SignalRW, TableWrite, TextWrite
from softioc import alarm, builder, fields
from softioc.imports import db_put_field
from softioc.pythonSoftIoc import RecordWrapper

from ._pvi import Pvi, PviGroup
from ._types import (
    EpicsName,
    InErrorException,
    RecordInfo,
    RecordValue,
    check_num_labels,
    epics_to_panda_name,
    trim_description,
)

UnpackedArray = Union[
    npt.NDArray[np.int32], npt.NDArray[np.uint8], npt.NDArray[np.uint16]
]


@dataclass
class TableRecordWrapper:
    """Replacement RecordWrapper for controlling Tables.
    This is only expected to be used for MODE records."""

    record: RecordWrapper
    table_updater: "TableUpdater"

    def update_table(self, values: List[str]) -> None:
        """Set the given values into the table records"""
        self.table_updater.update_table(values)

    def __getattr__(self, name):
        """Forward all requests for other attributes to the underlying Record"""
        return getattr(self.record, name)


@dataclass
class TableFieldRecordContainer:
    """Associates a TableFieldDetails with RecordInfo, and thus its WaveformOut
    record."""

    field: TableFieldDetails
    record_info: Optional[RecordInfo]


def make_bit_order(
    table_field_records: Dict[str, TableFieldRecordContainer]
) -> Dict[str, TableFieldRecordContainer]:
    return dict(
        sorted(table_field_records.items(), key=lambda item: item[1].field.bit_low)
    )


class TableModeEnum(Enum):
    """Operation modes for the MODES record on PandA table fields"""

    VIEW = 0  # Discard all EPICS record updates, process all PandA updates (default)
    EDIT = 1  # Process all EPICS record updates, discard all PandA updates
    SUBMIT = 2  # Push EPICS records to PandA, overriding current PandA data
    DISCARD = 3  # Discard all EPICS records, re-fetch from PandA


class TableUpdater:
    """Class to handle creating and updating tables."""

    client: AsyncioClient
    table_name: EpicsName
    field_info: TableFieldInfo
    # Collection of the records that comprise the table's fields.
    # Order is exactly that which PandA sent.
    table_fields_records: typing.OrderedDict[str, TableFieldRecordContainer]
    # Collection of the records that comprise the SCALAR records for each field
    table_scalar_records: Dict[EpicsName, RecordInfo] = {}
    all_values_dict: Dict[EpicsName, RecordValue]

    def __init__(
        self,
        client: AsyncioClient,
        table_name: EpicsName,
        field_info: TableFieldInfo,
        all_values_dict: Dict[EpicsName, RecordValue],
    ):
        """Create all the table records

        Args:
           client: The client to be used to read/write to the PandA
           table_name: The name of the table, in EPICS format, e.g. "SEQ1:TABLE"
           field_info: The TableFieldInfo structure for this table
           all_values_dict: The pointer to the global dictionary containing the most
                recent value of all records as returned from GetChanges. This dict will
                be dynamically updated by other methods."""

        self.client = client
        self.table_name = table_name
        self.field_info = field_info
        pva_table_name = RecordName(table_name)

        # Make a labels field
        block, field = table_name.split(":", maxsplit=1)
        columns: RecordWrapper = builder.WaveformOut(
            table_name + ":LABELS",
            initial_value=np.array([k.encode() for k in field_info.fields]),
        )
        columns.add_info(
            "Q:group",
            {
                pva_table_name: {
                    "+id": "epics:nt/NTTable:1.0",
                    "labels": {"+type": "plain", "+channel": "VAL"},
                }
            },
        )
        pv_rec = builder.longStringIn(
            table_name + ":PV",
            initial_value=pva_table_name,
        )
        pv_rec.add_info(
            "Q:group",
            {
                RecordName(f"{block}:PVI"): {
                    f"pvi.{field.lower().replace(':', '_')}.rw": {
                        "+channel": "VAL",
                        "+type": "plain",
                    }
                },
            },
        )

        self.table_fields_records = OrderedDict(
            {
                k: TableFieldRecordContainer(v, None)
                for k, v in field_info.fields.items()
            }
        )
        self.all_values_dict = all_values_dict

        # The PVI group to put all records into
        pvi_group = PviGroup.PARAMETERS
        Pvi.add_pvi_info(
            table_name,
            pvi_group,
            SignalRW(table_name, table_name, TableWrite([])),
        )

        # The INDEX record's starting value
        DEFAULT_INDEX = 0

        # Note that the table_updater's table_fields are guaranteed sorted in bit order,
        # unlike field_info's fields. This means the record dict inside the table
        # updater are also in the same bit order.
        value = all_values_dict[table_name]
        assert isinstance(value, list)
        field_data = words_to_table(value, field_info)

        for i, (field_name, field_record_container) in enumerate(
            self.table_fields_records.items()
        ):
            field_details = field_record_container.field

            full_name = table_name + ":" + field_name
            full_name = EpicsName(full_name)
            description = trim_description(field_details.description, full_name)

            waveform_val = self._construct_waveform_val(
                field_data, field_name, field_details
            )

            field_record: RecordWrapper = builder.WaveformOut(
                full_name,
                DESC=description,
                validate=self.validate_waveform,
                on_update_name=self.update_waveform,
                initial_value=waveform_val,
                length=field_info.max_length,
            )

            field_pva_info = {
                "+type": "plain",
                "+channel": "VAL",
                "+putorder": i + 1,
                "+trigger": "",
            }

            pva_info = {f"value.{field_name.lower()}": field_pva_info}

            # For the last column in the table
            if i == len(self.table_fields_records) - 1:
                # Trigger a monitor update
                field_pva_info["+trigger"] = "*"
                # Add metadata
                pva_info[""] = {"+type": "meta", "+channel": "VAL"}

            field_record.add_info(
                "Q:group",
                {pva_table_name: pva_info},
            )

            field_record_container.record_info = RecordInfo(lambda x: x, None, False)

            field_record_container.record_info.add_record(field_record)

            # Scalar record gives access to individual cell in a column,
            # in combination with the INDEX record defined below
            scalar_record_name = EpicsName(full_name + ":SCALAR")

            scalar_record_desc = "Scalar val (set by INDEX rec) of column"
            # No better default than zero, despite the fact it could be a valid value
            # PythonSoftIOC issue #53 may alleviate this.
            initial_value = (
                field_data[field_name][DEFAULT_INDEX]
                if len(field_data[field_name]) > 0
                else 0
            )

            # Three possible field types, do per-type config
            if field_details.subtype == "int":
                scalar_record: RecordWrapper = builder.longIn(
                    scalar_record_name,
                    initial_value=initial_value,
                    DESC=scalar_record_desc,
                )

            elif field_details.subtype == "uint":
                assert initial_value >= 0, (
                    f"initial value {initial_value} for uint record "
                    f"{scalar_record_name} was negative"
                )
                scalar_record = builder.longIn(
                    scalar_record_name,
                    initial_value=initial_value,
                    DESC=scalar_record_desc,
                )

            elif field_details.subtype == "enum":
                assert field_details.labels

                check_num_labels(field_details.labels, scalar_record_name)
                scalar_record = builder.mbbIn(
                    scalar_record_name,
                    *field_details.labels,
                    initial_value=initial_value,
                    DESC=scalar_record_desc,
                )

            else:
                logging.error(
                    f"Unknown table field subtype {field_details.subtype} detected "
                    f"on table {table_name} field {field_name}. Using defaults."
                )
                scalar_record = builder.longIn(
                    scalar_record_name,
                    initial_value=initial_value,
                    DESC=scalar_record_desc,
                )

            Pvi.add_pvi_info(
                scalar_record_name,
                pvi_group,
                SignalRW(scalar_record_name, scalar_record_name, TextWrite()),
            )

            self.table_scalar_records[scalar_record_name] = RecordInfo(
                lambda x: x, None, False
            )

            self.table_scalar_records[scalar_record_name].add_record(scalar_record)

        # Create the mode record that controls when to Put back to PandA
        labels = [x.name for x in TableModeEnum]
        mode_record_name = EpicsName(table_name + ":" + "MODE")

        mode_record: RecordWrapper = builder.mbbOut(
            mode_record_name,
            *labels,
            DESC="Controls PandA <-> EPICS data interface",
            initial_value=TableModeEnum.VIEW.value,
            on_update=self.update_mode,
        )
        Pvi.add_pvi_info(
            mode_record_name,
            pvi_group,
            SignalRW(mode_record_name, mode_record_name, ComboBox()),
        )

        self.mode_record_info = RecordInfo(lambda x: x, labels, False)
        self.mode_record_info.add_record(mode_record)

        # Re-wrap the record itself so that GetChanges can access this TableUpdater
        self.mode_record_info.record = TableRecordWrapper(
            self.mode_record_info.record, self
        )
        # PVA needs a record to start and finish processing, but these don't need
        # putting on a screen
        for action in (TableModeEnum.EDIT, TableModeEnum.SUBMIT):
            action_record = builder.records.ao(
                mode_record_name + ":" + action.name,
                VAL=action.value,
                MDEL=-1,
                OUT=PP(mode_record),
            )
            # Edit mode done first, Submit mode done last
            putorder = (
                0 if action == TableModeEnum.EDIT else len(self.table_fields_records)
            )
            action_record.add_info(
                "Q:group",
                {
                    pva_table_name: {
                        f"_{action.name.lower()}": {
                            "+type": "proc",
                            "+channel": "PROC",
                            "+putorder": putorder,
                            "+trigger": "",
                        }
                    }
                },
            )

        # Index record specifies which element the scalar records should access
        index_record_name = EpicsName(table_name + ":INDEX")
        self.index_record = builder.longOut(
            index_record_name,
            DESC="Index for all SCALAR records on table",
            initial_value=DEFAULT_INDEX,
            on_update=self.update_index,
            DRVL=0,
            DRVH=field_data[field_name].size - 1,
        )

        Pvi.add_pvi_info(
            index_record_name,
            pvi_group,
            SignalRW(index_record_name, index_record_name, TextWrite()),
        )

    def validate_waveform(self, record: RecordWrapper, new_val) -> bool:
        """Controls whether updates to the waveform records are processed, based on the
        value of the MODE record.

        Args:
            record: The record currently being validated
            new_val: The new value attempting to be written

        Returns:
            bool: `True` to allow record update, `False` otherwise.
        """

        record_val = self.mode_record_info.record.get()

        if record_val == TableModeEnum.VIEW.value:
            logging.debug(
                f"{self.table_name} MODE record is VIEW, stopping update "
                f"to {record.name}"
            )
            return False
        elif record_val == TableModeEnum.EDIT.value:
            logging.debug(
                f"{self.table_name} MODE record is EDIT, allowing update "
                f"to {record.name}"
            )
            return True
        elif record_val == TableModeEnum.SUBMIT.value:
            # SUBMIT only present when currently writing out data to PandA.
            logging.warning(
                f"Update of record {record.name} attempted while MODE was SUBMIT."
                "New value will be discarded"
            )
            return False
        elif record_val == TableModeEnum.DISCARD.value:
            # DISCARD only present when currently overriding local data with PandA data
            logging.warning(
                f"Update of record {record.name} attempted while MODE was DISCARD."
                "New value will be discarded"
            )
            return False
        else:
            logging.error("MODE record has unknown value: " + str(record_val))
            # In case it isn't already, set an alarm state on the record
            self.mode_record_info.record.set_alarm(alarm.INVALID_ALARM, alarm.UDF_ALARM)
            return False

    async def update_waveform(self, new_val: int, record_name: str) -> None:
        """Handles updates to a specific waveform record to update its associated
        scalar value record"""
        self._update_scalar(record_name)

    async def update_mode(self, new_val: int):
        """Controls the behaviour when the MODE record is updated.
        Controls Put'ting data back to PandA, or re-Get'ting data from Panda
        and replacing record data."""

        assert self.mode_record_info.labels

        packed_data: List[str] = []
        new_label = self.mode_record_info.labels[new_val]

        if new_label == TableModeEnum.SUBMIT.name:
            try:
                # Send all EPICS data to PandA
                logging.info(f"Sending table data for {self.table_name} to PandA")

                table = {}
                for x in self.table_fields_records:
                    record_info = self.table_fields_records[x].record_info
                    if record_info:
                        table[x] = record_info.record.get()

                packed_data = table_to_words(table, self.field_info)

                panda_field_name = epics_to_panda_name(self.table_name)
                await self.client.send(Put(panda_field_name, packed_data))

            except Exception:
                logging.exception(
                    f"Unable to Put record {self.table_name}, value {packed_data},"
                    "to PandA. Rolling back to last value from PandA.",
                )

                # Reset value of all table records to last values returned from
                # GetChanges
                assert self.table_name in self.all_values_dict
                old_val = self.all_values_dict[self.table_name]

                if isinstance(old_val, InErrorException):
                    # If PythonSoftIOC issue #53 is fixed we could put some error state.
                    logging.error(
                        f"Cannot restore previous value to table {self.table_name}, "
                        "PandA marks this field as in error."
                    )
                    return

                assert isinstance(old_val, list)

                field_data = words_to_table(old_val, self.field_info)
                for field_name, field_record in self.table_fields_records.items():
                    assert field_record.record_info
                    # Table records are never In type, so can always disable processing
                    field_record.record_info.record.set(
                        field_data[field_name], process=False
                    )
            finally:
                # Already in on_update of this record, so disable processing to
                # avoid recursion
                self.mode_record_info.record.set(
                    TableModeEnum.VIEW.value, process=False
                )

        elif new_label == TableModeEnum.DISCARD.name:
            # Recreate EPICS data from PandA data
            logging.info(f"Re-fetching table {self.table_name} data from PandA")
            panda_field_name = epics_to_panda_name(self.table_name)
            panda_vals = await self.client.send(GetMultiline(f"{panda_field_name}"))

            field_data = words_to_table(panda_vals, self.field_info)

            for field_name, field_record in self.table_fields_records.items():
                assert field_record.record_info
                field_record.record_info.record.set(
                    field_data[field_name], process=False
                )

            # Already in on_update of this record, so disable processing to
            # avoid recursion
            self.mode_record_info.record.set(TableModeEnum.VIEW.value, process=False)

    def _construct_waveform_val(
        self,
        field_data: Dict[str, UnpackedArray],
        field_name: str,
        field_details: TableFieldDetails,
    ):
        """Convert the values into the right form. For enums this means converting
        the numeric values PandA sends us into the string representation. For all other
        types the numeric representation is used."""
        if field_details.labels:
            max_length = max([len(x) for x in field_details.labels])
            return np.array(
                [field_details.labels[x] for x in field_data[field_name]],
                dtype=f"<U{max_length + 1}",
            )
        return field_data[field_name]

    def update_table(self, new_values: List[str]) -> None:
        """Update the waveform records with the given values from the PandA, depending
        on the value of the table's MODE record.
        Note: This is NOT a method called through a record's `on_update`.

        Args:
            new_values: The list of new values from the PandA
        """

        curr_mode = TableModeEnum(self.mode_record_info.record.get())

        if curr_mode == TableModeEnum.VIEW:
            field_data = words_to_table(new_values, self.field_info)

            for field_name, field_record in self.table_fields_records.items():
                assert field_record.record_info

                waveform_val = self._construct_waveform_val(
                    field_data, field_name, field_record.field
                )

                # Must skip processing as the validate method would reject the update
                field_record.record_info.record.set(waveform_val, process=False)
                self._update_scalar(field_record.record_info.record.name)

            # All items in field_data have the same length, so just use 0th.
            self._update_index_drvh(list(field_data.values())[0])
        else:
            # No other mode allows PandA updates to EPICS records
            logging.warning(
                f"Update of table {self.table_name} attempted when MODE "
                "was not VIEW. New value will be discarded"
            )

    async def update_index(self, new_val) -> None:
        """Update the SCALAR record for every column in the table based on the new
        index and/or new waveform data."""
        for field_record in self.table_fields_records.values():
            assert field_record.record_info
            self._update_scalar(field_record.record_info.record.name)

    def _update_scalar(self, waveform_record_name: str) -> None:
        """Update the column's SCALAR record based on the new index and/or new waveform
        data.

        Args:
            waveform_record_name: The name of the column record including leading
            namespace, e.g. "<namespace>:SEQ1:TABLE:POSITION"
        """

        # Remove namespace from record name
        _, waveform_record_name = waveform_record_name.split(":", maxsplit=1)
        _, field_name = waveform_record_name.rsplit(":", maxsplit=1)

        record_info = self.table_fields_records[field_name].record_info
        assert record_info
        waveform_data = record_info.record.get()

        scalar_record = self.table_scalar_records[
            EpicsName(waveform_record_name + ":SCALAR")
        ].record

        index = self.index_record.get()

        labels = self.table_fields_records[field_name].field.labels

        try:
            scalar_val = waveform_data[index]
            if labels:
                # mbbi/o records must use the numeric index
                scalar_val = labels.index(scalar_val)
            sev = alarm.NO_ALARM
        except IndexError as e:
            logging.warning(
                f"Index {index} of record {waveform_record_name} is out of bounds.",
                exc_info=e,
            )
            scalar_val = 0
            sev = alarm.INVALID_ALARM
        except ValueError as e:
            logging.warning(
                f"Value {scalar_val} of record {waveform_record_name} is not "
                "a recognised value.",
                exc_info=e,
            )
            scalar_val = 0
            sev = alarm.INVALID_ALARM

        # alarm value is ignored if severity = NO_ALARM. Softioc also defaults
        # alarm value to UDF_ALARM, but I'm specifying it for clarity.
        scalar_record.set(scalar_val, severity=sev, alarm=alarm.UDF_ALARM)

    def _update_index_drvh(self, data: UnpackedArray):
        """Set the DRVH value of the index record based on the newly set data length"""
        # Note the -1 to account for zero indexing
        c_data = np.require(data.size - 1, dtype=np.int32)
        db_put_field(
            f"{self.index_record.name}.DRVH",
            fields.DBF_LONG,
            c_data.ctypes.data,
            1,
        )
