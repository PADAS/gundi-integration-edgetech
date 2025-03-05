import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pydantic

from app.actions.buoy import BuoyClient, ObservationSubject
from app.actions.edgetech.types import Buoy

logger = logging.getLogger(__name__)


class EdgetTechProcessor:
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
            data (List[dict]): A list of dictionaries representing raw buoy data records.
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

        The default filters define a time window starting 180 days before the current UTC time
        and ending at the current UTC time.

        Returns:
            Dict[str, any]: A dictionary with 'start_date' and 'end_date' keys defining the filter window.
        """
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=180)
        return {"start_date": start_date, "end_date": end_date}

    def _filter_data(self) -> List[Buoy]:
        """
        Filter buoy data records based on the defined time window.

        Iterates over the parsed buoy data and selects records whose 'currentState.lastUpdated'
        timestamp falls within the filter's start_date and end_date range.

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

    def _get_newest_buoy_states(self) -> Dict[str, Buoy]:
        """
        Retrieve the most recent state for each unique buoy based on serial number.

        Filters the buoy data and then selects the buoy record with the latest 'lastUpdated'
        timestamp for each serial number.

        Returns:
            Dict[str, Buoy]: A dictionary mapping each buoy's serial number to its newest Buoy record.
        """
        filtered_data = self._filter_data()
        newest_by_serial = {}
        for buoy in filtered_data:
            serial = buoy.currentState.serialNumber
            current_state = buoy.currentState
            current_newest = newest_by_serial.get(serial)
            if (
                current_newest is None
            ) or current_state.lastUpdated > current_newest.currentState.lastUpdated:
                newest_by_serial[serial] = buoy
        return newest_by_serial

    def _are_equivalent(self, er_subject: ObservationSubject, buoy: Buoy) -> bool:
        """
        Determine whether an ER subject and a Buoy record represent equivalent device states.

        The comparison checks the following:
            1. Matching serial numbers (handling a '_A' or '_B' suffix if present).
            2. Matching active state versus deletion status.
            3. Matching geographic location if available.

        Args:
            er_subject (ObservationSubject): The ER system's subject representing a device observation.
            buoy (Buoy): The buoy object containing current state information.

        Returns:
            bool: True if the ER subject and the buoy record are equivalent; otherwise, False.
        """
        # (1) Serial Number:
        if er_subject.name.startswith(self._prefix):
            obs_serial = er_subject.name[len(self._prefix) :]
        else:
            obs_serial = er_subject.name

        # Remove potential suffix "_A" or "_B" if present
        if obs_serial.endswith("_A") or obs_serial.endswith("_B"):
            obs_serial = obs_serial[:-2]

        if obs_serial != buoy.serialNumber:
            return False

        if obs_serial != buoy.currentState.serialNumber:
            return False

        # (2) Active state vs. deletion:
        if er_subject.is_active != (
            not buoy.currentState.isDeleted or buoy.currentState.isDeployed
        ):
            return False

        # (3) Compare location:
        if (
            er_subject.last_position
            and er_subject.last_position.geometry
            and er_subject.last_position.geometry.coordinates
        ):
            obs_lon, obs_lat = er_subject.last_position.geometry.coordinates

            # Determine which buoy coordinates to compare based on the subject suffix.
            if er_subject.name.endswith("_B"):
                buoy_lat = buoy.currentState.endLatDeg
                buoy_lon = buoy.currentState.endLonDeg
            else:
                buoy_lat = buoy.currentState.latDeg
                buoy_lon = buoy.currentState.lonDeg

            if buoy_lat is None or buoy_lon is None:
                return False

            tolerance = 0.0001
            if (
                abs(obs_lat - buoy_lat) > tolerance
                or abs(obs_lon - buoy_lon) > tolerance
            ):
                return False
        else:
            # If ER subject has no location, then the buoy should also have no primary location.
            if (
                buoy.currentState.latDeg is not None
                or buoy.currentState.lonDeg is not None
            ):
                return False

        return True

    async def process(self) -> List[dict]:
        """
        Process buoy data to generate observation events for the ER system.

        This asynchronous method performs the following steps:
            1. Retrieves the latest buoy states grouped by serial number.
            2. Fetches existing ER subjects from the ER client.
            3. Maps ER subjects by name and categorizes buoy states into:
                - Inserts: Buoys not present in ER.
                - Updates: Buoys present in ER that require updating.
                - No-ops: Buoys present in ER with equivalent data.
            4. Generates observation events for new or updated buoys.
            5. Logs information about the inserts, updates, and no-ops.

        Returns:
            List[dict]: A list of dictionaries representing the observation events generated during processing.

        Raises:
            pydantic.ValidationError: If validation fails when creating buoy observation events.
        """
        buoy_states = self._get_newest_buoy_states()
        er_subjects = await self._er_client.get_er_subjects()

        # Create a map of ER subjects by name.
        er_subject_mapping = {subject.name: subject for subject in er_subjects}
        er_subject_names = set(er_subject_mapping.keys())

        # Categorize buoys into insert, update, and no-op
        insert_buoys = set()
        update_buoys = set()
        noop_buoys = set()

        observations = []

        for serial_number, buoy_state in buoy_states.items():
            if (
                buoy_state.currentState.latDeg is None
                or buoy_state.currentState.lonDeg is None
            ):
                logger.warning(
                    f"Skipping buoy {serial_number} with missing location data"
                )
                continue

            primary_subject_name = f"{self._prefix}{serial_number}_A"
            if primary_subject_name in er_subject_names:
                # Buoy exists in ER, so update it.
                update_buoys.add(serial_number)
            else:
                # Buoy does not exist in ER, insert it.
                insert_buoys.add(serial_number)

        # Process inserts.
        for serial_number in insert_buoys:
            buoy_state = buoy_states[serial_number]
            logger.info(f"Inserting new buoy: {serial_number}")
            try:
                new_observations = buoy_state.create_observations(self._prefix)
                observations.extend(new_observations)
            except pydantic.ValidationError as ve:
                logger.exception(
                    "Failed making BuoyEvent for %s. Error: %s",
                    serial_number,
                    ve.json(),
                )

        # Process updates.
        for serial_number in update_buoys:
            buoy_state = buoy_states[serial_number]
            primary_subject_name = f"{self._prefix}{serial_number}_A"
            er_subject = er_subject_mapping.get(primary_subject_name)
            if er_subject is None:
                logger.warning(
                    f"Primary ER subject not found for buoy {serial_number}. Skipping update."
                )
                continue
            if self._are_equivalent(er_subject, buoy_state):
                # No-op: data is already up to date.
                noop_buoys.add(serial_number)
                logger.info(f"No changes needed for buoy: {serial_number}")
            else:
                # Update ER subject.
                logger.info(f"Updating buoy: {serial_number}")
                try:
                    new_observations = buoy_state.create_observations(self._prefix)
                    observations.extend(new_observations)
                except pydantic.ValidationError as ve:
                    logger.exception(
                        "Failed updating BuoyEvent for %s. Error: %s",
                        serial_number,
                        ve.json(),
                    )

        logger.info(f"Inserts: {insert_buoys}")
        logger.info(f"Updates: {update_buoys - noop_buoys}")
        logger.info(f"No-ops: {noop_buoys}")

        return observations
