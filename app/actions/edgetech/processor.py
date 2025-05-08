import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import pydantic

from app.actions.buoy import BuoyClient, ObservationSubject
from app.actions.edgetech.types import Buoy

logger = logging.getLogger(__name__)


class EdgeTechProcessor:
    def __init__(
        self,
        data: List[Buoy],
        er_token: str,
        er_url: str,
        filters: Optional[dict] = None,
    ):
        """
        Initialize an EdgetTechProcessor instance.

        This constructor parses raw data records into Buoy objects, initializes an ER client for
        further processing, and sets up filters for processing buoy data.

        Args:
            data (List[Buoy]): A list of Buoy objects (parsed from raw data).
            er_token (str): Authentication token for the ER client.
            er_url (str): URL endpoint for the ER client.
            filters (dict, optional): A dictionary containing filter criteria (e.g., start_date and end_date).
                                      If not provided, default filters covering the last 180 days are used.
        """
        self._data = [Buoy.parse_obj(record) for record in data]
        self._er_client = BuoyClient(er_token, er_url)
        self._filters = filters or self._get_default_filters()
        self._prefix = "edgetech_"

    def _get_default_filters(self) -> Dict[str, Any]:
        """
        Generate default filter criteria for processing buoy data.

        By default, this method defines a time window starting 180 days before the current UTC time
        and ending at the current UTC time.

        Returns:
            Dict[str, Any]: A dictionary with 'start_date' and 'end_date' keys defining the filter window.
        """
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=180)
        return {"start_date": start_date, "end_date": end_date}

    def _filter_data(self) -> List[Buoy]:
        """
        Filter buoy data records based on the time window defined in self._filters.

        Iterates over the parsed buoy data and selects records whose currentState.lastUpdated
        timestamp falls within the filter's start_date and end_date.

        Returns:
            List[Buoy]: A list of Buoy objects that satisfy the filter criteria.
        """
        filtered_data: List[Buoy] = []
        for record in self._data:
            record_current_state = record.currentState.lastUpdated
            if (
                self._filters["start_date"].timestamp()
                <= record_current_state.timestamp()
                <= self._filters["end_date"].timestamp()
            ):
                filtered_data.append(record)
        return filtered_data

    def _group_by_serial_number(self) -> Dict[str, List[Buoy]]:
        """
        Group the filtered Buoy data by serial number, returning a dictionary in which each key
        is a serial number and each value is a list of Buoy records for that serial number.

        The lists are sorted in ascending order by each Buoy's currentState.lastUpdated.

        Returns:
            Dict[str, List[Buoy]]: A dictionary mapping each serial number to a sorted list of Buoy objects.
        """
        filtered_data = self._filter_data()
        buoy_states: Dict[str, List[Buoy]] = {}

        for record in filtered_data:
            serial_number = record.serialNumber
            if serial_number not in buoy_states:
                buoy_states[serial_number] = [record]
            else:
                buoy_states[serial_number].append(record)

        for serial_number, buoy_list in buoy_states.items():
            buoy_list.sort(key=lambda x: x.currentState.lastUpdated, reverse=False)

        return buoy_states

    async def _get_er_subject_mapping(self) -> Dict[str, Any]:
        """
        Retrieves existing ER subjects from the ER client and returns a mapping by name.
        """
        er_subjects = await self._er_client.get_er_subjects()
        er_subject_mapping = {subject.name: subject for subject in er_subjects}
        return er_subject_mapping

    def _has_location_data(self, buoy_states: List[Buoy]) -> bool:
        """
        Checks if at least one buoy record has location data.
        """
        for buoy_state in buoy_states:
            current = buoy_state.currentState
            # Quick check for direct location data
            if (
                current.latDeg is not None
                or current.lonDeg is not None
                or current.recoveredLatDeg is not None
                or current.recoveredLonDeg is not None
            ):
                return True

            # Otherwise, check the changeRecords
            for changeRecord in buoy_state.changeRecords:
                if changeRecord.type == "MODIFY":
                    for change in changeRecord.changes:
                        if change.key in [
                            "latDeg",
                            "lonDeg",
                            "endLatDeg",
                            "endLonDeg",
                            "recoveredLatDeg",
                            "recoveredLonDeg",
                        ]:
                            return True
        return False

    def _identify_buoys(
        self,
        buoy_states_by_serial_number: Dict[str, List[Buoy]],
        er_subject_mapping: Dict[str, Any],
    ) -> Tuple[Set[str], Set[str], Set[str]]:
        """
        Determines which buoys need to be inserted, updated, or have no location data.
        """
        er_subject_names = set(er_subject_mapping.keys())
        insert_buoys = set()
        update_buoys = set()
        no_location_buoys = set()

        for serial_number, buoy_states in buoy_states_by_serial_number.items():
            # Check location
            if not self._has_location_data(buoy_states):
                no_location_buoys.add(serial_number)
                continue

            primary_subject_name = f"{self._prefix}{serial_number}_A"
            if primary_subject_name in er_subject_names:
                update_buoys.add(serial_number)
            else:
                insert_buoys.add(serial_number)

        return insert_buoys, update_buoys, no_location_buoys

    def _process_inserts(
        self, insert_buoys: set, buoy_states_by_serial_number: Dict[str, List[Buoy]]
    ) -> List[Dict[str, Any]]:
        """
        Creates observation events for buoys that need insertion.
        """
        observations = []
        for serial_number in insert_buoys:
            buoy_records = buoy_states_by_serial_number[serial_number]
            last_known_position = None
            last_known_end_position = None

            for buoy_record in buoy_records:
                try:
                    new_obs, last_known_position, last_known_end_position = (
                        buoy_record.create_observations(
                            prefix=self._prefix,
                            subject_status=None,
                            last_know_position_previous_states=last_known_position,
                            last_know_end_position_previous_states=last_known_end_position,
                            was_part_of_trawl=None,
                        )
                    )
                    observations.extend(new_obs)
                except pydantic.ValidationError as ve:
                    logger.exception(
                        "Failed to create BuoyEvent for %s. Error: %s",
                        serial_number,
                        ve.json(),
                    )
        return observations

    def _process_updates(
        self,
        update_buoys: set,
        buoy_states_by_serial_number: Dict[str, List[Buoy]],
        er_subject_mapping: Dict[str, Any],
        noop_buoys: set,
    ) -> List[Dict[str, Any]]:
        """
        Creates observation events for buoys that need updating, skipping those with no changes (no-op).
        """
        observations = []
        for serial_number in update_buoys:
            buoy_records = buoy_states_by_serial_number[serial_number]
            primary_subject_name = f"{self._prefix}{serial_number}_A"
            er_subject: Optional[ObservationSubject] = er_subject_mapping.get(
                primary_subject_name
            )
            secondary_subject_name = f"{self._prefix}{serial_number}_B"

            if er_subject is None:
                logger.warning(
                    f"Primary ER subject not found for buoy {serial_number}. Skipping update."
                )
                continue

            was_part_of_trawl = secondary_subject_name in er_subject_mapping

            last_known_position, last_known_end_position = None, None
            buoys_observations = []
            for buoy_record in buoy_records:
                try:
                    new_obs, last_known_position, last_known_end_position = (
                        buoy_record.create_observations(
                            prefix=self._prefix,
                            subject_status=er_subject.is_active,
                            last_observation_timestamp=er_subject.last_position_date,
                            last_know_position_previous_states=last_known_end_position,
                            last_know_end_position_previous_states=last_known_position,
                            was_part_of_trawl=was_part_of_trawl,
                        )
                    )
                    buoys_observations.extend(new_obs)
                except pydantic.ValidationError as ve:
                    logger.exception(
                        "Failed to update BuoyEvent for %s. Error: %s",
                        serial_number,
                        ve.json(),
                    )

            if len(buoys_observations) == 0:
                noop_buoys.add(serial_number)
            observations.extend(buoys_observations)

        return observations

    async def process(self) -> Tuple[List[Dict[str, Any]], Set[str], Set[str]]:
        """
        Process buoy data to generate observation events for the ER system.

        This asynchronous method performs the following steps:
            1. Retrieves and filters the latest buoy states grouped by serial number.
            2. Fetches existing ER subjects from the ER client.
            3. Maps ER subjects by name and categorizes buoy states into:
                - Inserts: Buoys not yet present in ER.
                - Updates: Buoys present in ER that need to be updated.
                - No-ops: Buoys with no location data or no changes.
            4. Creates observation events for new or updated buoys.
            5. Logs information about inserts, updates, and no-ops.

        Returns:
            List[dict]: A list of dictionaries representing the observation events generated during processing.
            Set[str]: A set of serial numbers for buoys that were inserted.
            Set[str]: A set of serial numbers for buoys that were updated.
        Raises:
            pydantic.ValidationError: If validation fails while creating buoy observation events.
        """
        buoy_states_by_serial_number = self._group_by_serial_number()
        er_subject_mapping = await self._get_er_subject_mapping()

        # Identify buoys for insertion or update (or no location data)
        insert_buoys, update_buoys, no_location_buoys = self._identify_buoys(
            buoy_states_by_serial_number, er_subject_mapping
        )
        noop_buoys = set()

        logger.warning(
            f"Skipping {len(no_location_buoys)} serial numbers without location data: {no_location_buoys}"
        )

        # Process inserts
        insert_observations = self._process_inserts(
            insert_buoys, buoy_states_by_serial_number
        )

        # Process updates
        update_observations = self._process_updates(
            update_buoys,
            buoy_states_by_serial_number,
            er_subject_mapping,
            noop_buoys,
        )

        # Final sets
        update_buoys = update_buoys - noop_buoys

        logger.info(f"Inserts: {insert_buoys}")
        logger.info(f"Updates: {update_buoys}")
        logger.info(f"No-op: {noop_buoys}")

        observations = insert_observations + update_observations
        return observations, insert_buoys, update_buoys
