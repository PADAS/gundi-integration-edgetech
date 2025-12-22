import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import uuid4

import pydantic

from app.actions.buoy import BuoyClient
from app.actions.buoy.types import BuoyGear
from app.actions.edgetech.types import Buoy
from app.actions.utils import get_hashed_user_id

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

        By default, this method defines a time window starting 30 minutes before the current UTC time.

        Returns:
            Dict[str, Any]: A dictionary with 'start_datetime' keys defining the filter window.
        """
        start_datetime = datetime.now(timezone.utc) - timedelta(minutes=30)
        return {"start_datetime": start_datetime}

    def _remove_milliseconds(self, dt: datetime) -> datetime:
        """
        Remove milliseconds from a datetime object.

        Args:
            dt: The datetime object to process.
        """
        return dt.replace(microsecond=0)

    async def _create_gear_payload(
        self,
        buoy: Buoy,
        device_status: str,
        manufacturer_id_to_source_id: Dict[str, str],
        end_unit_buoy: Optional[Buoy] = None,
        set_id: Optional[str] = None,
        include_initial_deployment: bool = True,
    ) -> Dict[str, Any]:
        """
        Create a gear payload directly from Buoy data.

        Args:
            buoy: The main Buoy object
            device_status: Status of the device (deployed/hauled)
            manufacturer_id_to_source_id: Mapping of manufacturer_id to source_id for existing sources
            end_unit_buoy: Optional second buoy for two-unit lines
            set_id: Optional gear set ID (auto-generated if not provided)
            include_initial_deployment: Whether to include initial_deployment_date

        Returns:
            Dict in the format expected by /api/v1/gear/ POST endpoint
        """
        hashed_user_id = get_hashed_user_id(buoy.userId)

        last_updated = buoy.currentState.lastUpdated
        last_deployed = buoy.currentState.dateDeployed or last_updated
        
        # Create devices list
        devices = []
        
        # Main device
        main_device_id = f"{buoy.serialNumber}_{hashed_user_id}"

        secondary_device_id = None
        if buoy.currentState.endLatDeg and buoy.currentState.endLonDeg:
            main_device_id += "_A"
            secondary_device_id = f"{buoy.serialNumber}_{hashed_user_id}_B"
            secondary_latitude = buoy.currentState.endLatDeg
            secondary_longitude = buoy.currentState.endLonDeg
            secondary_last_deployed = buoy.currentState.dateDeployed or last_updated
            secondary_device_additional_data = json.loads(buoy.json())
            secondary_device_additional_data.pop("changeRecords", None)
        elif end_unit_buoy:
            secondary_device_id = f"{end_unit_buoy.serialNumber}_{hashed_user_id}"
            secondary_latitude = end_unit_buoy.currentState.latDeg
            secondary_longitude = end_unit_buoy.currentState.lonDeg
            secondary_last_deployed = end_unit_buoy.currentState.dateDeployed or last_updated
            secondary_device_additional_data = json.loads(end_unit_buoy.json())
            secondary_device_additional_data.pop("changeRecords", None)
        
        main_device = {
            "device_id": manufacturer_id_to_source_id.get(main_device_id) or str(uuid4()),
            "mfr_device_id": main_device_id,
            "last_deployed": self._remove_milliseconds(last_deployed).isoformat(),
            "last_updated": self._remove_milliseconds(last_updated).isoformat(),
            "device_status": device_status,
            "location": {
                "latitude": buoy.currentState.latDeg,
                "longitude": buoy.currentState.lonDeg,
            },
        }
        
        # Add raw data - convert to JSON-serializable format
        raw_data = json.loads(buoy.json())
        raw_data.pop("changeRecords", None)
        main_device["device_additional_data"] = raw_data
        
        devices.append(main_device)

        if secondary_device_id:
            secondary_device = {
                "device_id": manufacturer_id_to_source_id.get(secondary_device_id) or str(uuid4()),
                "mfr_device_id": secondary_device_id,
                "last_deployed": self._remove_milliseconds(secondary_last_deployed).isoformat(),
                "last_updated": self._remove_milliseconds(last_updated).isoformat(),
                "device_status": device_status,
                "location": {
                    "latitude": secondary_latitude,
                    "longitude": secondary_longitude,
                },
                "device_additional_data": secondary_device_additional_data
            }
            devices.append(secondary_device)
        
        # Determine deployment type
        deployment_type = "trawl" if len(devices) > 1 else "single"
        
        # Build payload
        payload = {
            "set_id": set_id or str(uuid4()),
            "owner_id": buoy.userId,
            "manufacturer_name": "EdgeTech",
            "deployment_type": deployment_type,
            "devices_in_set": len(devices),
            "devices": devices,
        }

        if include_initial_deployment:
            payload["initial_deployment_date"] = self._remove_milliseconds(last_deployed).isoformat()
        payload_json = json.dumps(payload, default=str)
        payload = json.loads(payload_json)
        return payload

    def _create_haul_payload(
        self,
        er_gear: BuoyGear,
        edgetech_buoy: Optional[Buoy] = None
    ) -> Dict[str, Any]:
        """
        Create a haul payload from an existing ER gear.
        
        Attempts to use recovery location from EdgeTech data if available,
        otherwise falls back to the last known deployed location from ER.

        Args:
            er_gear: The existing gear from ER
            edgetech_buoy: Optional EdgeTech buoy data with potential recovery location

        Returns:
            Dict in the format expected by /api/v1/gear/ POST endpoint
        """
        devices = []
        
        # Check if EdgeTech provides recovery location
        recovery_location_available = False
        recovery_lat = None
        recovery_lon = None
        
        if edgetech_buoy and edgetech_buoy.currentState.recoveredLatDeg and edgetech_buoy.currentState.recoveredLonDeg:
            recovery_location_available = True
            recovery_lat = edgetech_buoy.currentState.recoveredLatDeg
            recovery_lon = edgetech_buoy.currentState.recoveredLonDeg
            logger.info(
                f"Using recovery location from EdgeTech for gear {er_gear.display_id}: "
                f"({recovery_lat}, {recovery_lon})"
            )
        
        for device in er_gear.devices:
            # Use recovery location if available, otherwise use deployed location from ER
            if recovery_location_available:
                location_lat = recovery_lat
                location_lon = recovery_lon
            else:
                location_lat = device.location.latitude
                location_lon = device.location.longitude
                logger.info(
                    f"No recovery location available for device {device.mfr_device_id}, "
                    f"using last deployed location from ER: ({location_lat}, {location_lon})"
                )
            
            haul_device = {
                "device_id": device.device_id,
                "mfr_device_id": device.mfr_device_id,
                "last_deployed": device.last_deployed.isoformat() if device.last_deployed else device.last_updated.isoformat(),
                "last_updated": self._remove_milliseconds(datetime.now(timezone.utc)).isoformat(),
                "device_status": "hauled",
                "location": {
                    "latitude": location_lat,
                    "longitude": location_lon,
                },
            }
            devices.append(haul_device)
        
        payload = {
            "deployment_type": er_gear.type,
            "manufacturer_name": "EdgeTech",
            "set_id": er_gear.display_id,
            "devices": devices,
        }
        
        return payload

    def _is_hauled_or_recovered(self, record: Buoy) -> bool:
        """
        Determine if a buoy record indicates a hauled/recovered state.
        
        A buoy is considered hauled/recovered if:
        - isDeleted is True, OR
        - isDeployed is False, OR
        - dateRecovered is present
        
        Args:
            record (Buoy): The buoy record to check.
            
        Returns:
            bool: True if the buoy should be treated as hauled/recovered.
        """
        return (
            record.currentState.isDeleted or
            not record.currentState.isDeployed or
            record.currentState.dateRecovered is not None
        )
    
    def _should_skip_buoy(self, record: Buoy) -> Tuple[bool, Optional[str]]:
        """
        Determine if a buoy record should be skipped during processing.

        Buoys are kept in the dataset for processing if:
        1. They have location data (deployed or recovery location), OR
        2. They indicate a hauled/recovered state (even without recovery location)
        
        This ensures that hauled gears without recovery coordinates can still be 
        updated to hauled status in EarthRanger.

        Args:
            record (Buoy): The buoy record to check.

        Returns:
            Tuple[bool, Optional[str]]: A tuple containing:
                - bool: True if the record should be skipped, False otherwise
                - Optional[str]: The reason for skipping, or None if not skipped
        """
        # Don't skip if buoy should be hauled - we need to process it even without recovery location
        if self._is_hauled_or_recovered(record):
            if not record.has_location:
                logger.info(
                    f"Processing hauled/recovered buoy {record.serialNumber} without recovery location "
                    f"(isDeleted={record.currentState.isDeleted}, "
                    f"isDeployed={record.currentState.isDeployed}, "
                    f"dateRecovered={record.currentState.dateRecovered}). "
                    f"Will use fallback location from deployed state or ER."
                )
            return False, None
        
        # For deployed buoys, location data is required
        if not record.has_location:
            return (
                True,
                f"Skipping buoy record with serial number {record.serialNumber} that has no location data "
                f"and is not in hauled/recovered state. "
                f"Last updated at {record.currentState.lastUpdated}. "
                f"(isDeleted={record.currentState.isDeleted}, isDeployed={record.currentState.isDeployed})",
            )

        return False, None

    def _filter_edgetech_buoys_data(self, data: List[Buoy]) -> List[Buoy]:
        """
        Filter buoy data records to include only processable records.

        This method keeps:
        - Deployed/active buoys with location data
        - Deleted/non-deployed buoys (for haul detection), even without recovery location
        
        Only skips buoys that are deployed but lack location data.

        Returns:
            List[Buoy]: A list of Buoy objects that can be processed.
        """
        filtered_data: List[Buoy] = []
        skipped_serial_numbers: Set[str] = set()

        for record in data:
            should_skip, skip_reason = self._should_skip_buoy(record)

            if should_skip:
                logger.warning(skip_reason)
                skipped_serial_numbers.add(record.serialNumber)
                continue

            filtered_data.append(record)

        return filtered_data

    def _get_latest_buoy_states(self, data: List[Buoy]) -> List[Buoy]:
        """
        Retrieve the latest buoy states from the parsed data.

        Returns:
            List[Buoy]: A list of Buoy objects representing the latest states.
        """
        latest: Dict[str, Buoy] = {}

        for record in data:
            key = f"{record.serialNumber}{record.userId}"
            prev = latest.get(key)
            if (
                prev is None
                or record.currentState.lastUpdated > prev.currentState.lastUpdated
            ):
                latest[key] = record

        return list(latest.values())

    async def _identify_buoys(
        self,
        er_gears_devices_id_to_gear: Dict[str, BuoyGear],
        serial_number_to_edgetech_buoy: Dict[str, Buoy],
    ) -> Tuple[Set[str], Set[str], Set[str]]:
        """
        Determines which buoys need to be inserted (deployed), updated, or hauled.

        This method only processes buoys that appear in the current sync window from EdgeTech.
        It explicitly checks the buoy's status flags (isDeleted, isDeployed) to determine
        if a haul event should be generated, rather than inferring hauling from absence in the dataset.

        Process:
        - `to_deploy`: Buoys that are deployed in EdgeTech but not yet in ER.
        - `to_update`: Buoys that exist in ER and have changes (location, status, or newer data).
        - `to_haul`: Buoys that are explicitly marked as deleted or not deployed in EdgeTech.

        Args:
            er_gears_devices_id_to_gear (Dict[str, BuoyGear]): Mapping of ER device IDs to gear objects.
            serial_number_to_edgetech_buoy (Dict[str, Buoy]): Mapping of serial numbers to EdgeTech buoy objects.

        Returns:
            Tuple[Set[str], Set[str], Set[str]]: Three sets containing:
                - `to_deploy`: Serial numbers of buoys to deploy.
                - `to_haul`: Serial numbers of buoys to haul.
                - `to_update`: Serial numbers of buoys to update.
        """
        to_deploy: Set[str] = set()
        to_haul: Set[str] = set()
        to_update: Set[str] = set()

        # Process only the buoys present in the current sync window
        for serial_number_user_id in serial_number_to_edgetech_buoy.keys():
            serial_number, hashed_user_id = serial_number_user_id.split("/", 2)
            primary_subject_name = f"{serial_number}_{hashed_user_id}_A"
            standard_subject_name = f"{serial_number}_{hashed_user_id}"

            edgetech_buoy = serial_number_to_edgetech_buoy[serial_number_user_id]
            
            # Check if gear exists in ER
            er_gear = er_gears_devices_id_to_gear.get(
                primary_subject_name
            ) or er_gears_devices_id_to_gear.get(standard_subject_name)

            if er_gear is None:
                # Gear doesn't exist in ER - check if it should be deployed
                if edgetech_buoy.currentState.isDeployed and not edgetech_buoy.currentState.isDeleted:
                    to_deploy.add(serial_number_user_id)
                    logger.info(f"Buoy {serial_number_user_id} marked for deployment (not in ER, deployed in EdgeTech)")
                else:
                    logger.info(f"Buoy {serial_number_user_id} skipped (not in ER, not deployed or deleted in EdgeTech)")
            else:
                # Gear exists in ER - determine if it needs update or haul
                
                # Check for explicit haul conditions from EdgeTech
                if edgetech_buoy.currentState.isDeleted or not edgetech_buoy.currentState.isDeployed:
                    # Buoy is explicitly marked as deleted or not deployed in EdgeTech
                    # Only mark for haul if ER still shows it as deployed
                    if er_gear.status == "deployed":
                        to_haul.add(serial_number_user_id)
                        logger.info(
                            f"Buoy {serial_number_user_id} marked for haul "
                            f"(isDeleted={edgetech_buoy.currentState.isDeleted}, "
                            f"isDeployed={edgetech_buoy.currentState.isDeployed})"
                        )
                    else:
                        logger.info(f"Buoy {serial_number_user_id} already hauled in ER, skipping")
                else:
                    # Buoy is still deployed - check if it needs updating
                    edgetech_buoy_current_location = (
                        edgetech_buoy.currentState.latDeg, edgetech_buoy.currentState.lonDeg
                    )
                    er_gear_current_location = [
                        (device.location.latitude, device.location.longitude) for device in er_gear.devices
                    ]
                    location_changed = edgetech_buoy_current_location not in er_gear_current_location
                    
                    # Check if EdgeTech data is newer than ER data
                    edgetech_last_updated = edgetech_buoy.currentState.lastUpdated
                    er_last_updated = er_gear.last_updated
                    has_newer_data = edgetech_last_updated > er_last_updated
                    
                    if location_changed or has_newer_data:
                        to_update.add(serial_number_user_id)
                        logger.info(
                            f"Buoy {serial_number_user_id} marked for update "
                            f"(location_changed={location_changed}, has_newer_data={has_newer_data})"
                        )

        logger.info(f"Buoys to deploy: {len(to_deploy)} - {to_deploy}")
        logger.info(f"Buoys to haul: {len(to_haul)} - {to_haul}")
        logger.info(f"Buoys to update: {len(to_update)} - {to_update}")

        return to_deploy, to_haul, to_update

    async def process(self) -> List[Dict[str, Any]]:
        """
        Process buoy data to generate gear payloads for the Buoy API.

        This method processes only the buoys present in the current sync window from EdgeTech.
        It determines the appropriate action for each buoy based on its explicit status flags
        and comparison with the current state in EarthRanger.

        Processing steps:
            1. Retrieves and filters the latest buoy states grouped by serial number.
            2. Fetches existing ER gears and creates mappings for efficient lookups.
            3. Fetches all sources once for manufacturer ID to source ID mapping.
            4. Categorizes buoys based on explicit status checks:
                - Deploy: Buoys marked as deployed in EdgeTech but not yet in ER.
                - Update: Buoys in ER with location changes or newer data from EdgeTech.
                - Haul: Buoys explicitly marked as deleted or not deployed in EdgeTech 
                        while still showing as deployed in ER.
            5. Creates gear payloads directly for each operation.

        Important: Absence of a buoy from the sync window does NOT imply it was hauled.
        Haul events are only generated when EdgeTech explicitly marks a buoy as deleted
        or not deployed.

        Returns:
            List[dict]: A list of gear payloads ready to be sent to the Buoy API.
        """
        # Get the latest state for each buoy in the sync window
        # Note: self._data already contains only buoys updated within the sync window
        # as filtered by the EdgeTechClient based on start_datetime
        edgetech_deployed_buoys = self._get_latest_buoy_states(self._data)
        
        # Filter out buoys without location data (we keep deleted/non-deployed for haul detection)
        edgetech_deployed_buoys = self._filter_edgetech_buoys_data(edgetech_deployed_buoys)

        serial_number_to_edgetech_buoy = {
            f"{buoy.serialNumber}/{get_hashed_user_id(buoy.userId)}": buoy
            for buoy in edgetech_deployed_buoys
        }
        
        logger.info(
            f"Processing {len(serial_number_to_edgetech_buoy)} buoys from EdgeTech sync window"
        )

        # Fetch all sources once and create a mapping for efficient lookups
        logger.info("Fetching all sources from Buoy API...")
        sources = await self._er_client.get_sources(params={"page_size": 10000})
        manufacturer_id_to_source_id = {
            source.get("manufacturer_id"): source.get("id")
            for source in sources
            if source.get("manufacturer_id")
        }
        logger.info(f"Loaded {len(manufacturer_id_to_source_id)} source mappings")

        # Fetch all existing gears from ER to compare against EdgeTech data
        logger.info("Fetching all gears from EarthRanger...")
        er_gears = await self._er_client.get_er_gears(params={"page_size": 10000})

        er_gears_devices_id_to_gear = {
            device.mfr_device_id: gear
            for gear in er_gears
            for device in gear.devices
        }

        to_deploy, to_haul, to_update = await self._identify_buoys(
            er_gears_devices_id_to_gear,
            serial_number_to_edgetech_buoy,
        )

        gear_payloads = []

        # Process deployments (new gear sets)
        for serial_number_user_id in to_deploy:
            edgetech_buoy = serial_number_to_edgetech_buoy[serial_number_user_id]
            
            try:
                # Get end unit buoy if this is a two-unit line
                end_unit_buoy = None
                if edgetech_buoy.currentState.isTwoUnitLine and edgetech_buoy.currentState.endUnit:
                    end_unit_buoy_key = f"{edgetech_buoy.currentState.endUnit}/{get_hashed_user_id(edgetech_buoy.userId)}"
                    end_unit_buoy = serial_number_to_edgetech_buoy.get(end_unit_buoy_key)
                    
                    if not end_unit_buoy:
                        logger.warning(
                            "End unit buoy %s not found for serial number %s, skipping deployment.",
                            edgetech_buoy.currentState.endUnit,
                            serial_number_user_id,
                        )
                        continue
                    
                if edgetech_buoy.currentState.startUnit:
                    # This record is for the end unit, skip it (will be handled by start unit)
                    continue

                payload = await self._create_gear_payload(
                    buoy=edgetech_buoy,
                    device_status="deployed",
                    manufacturer_id_to_source_id=manufacturer_id_to_source_id,
                    end_unit_buoy=end_unit_buoy,
                    include_initial_deployment=True,
                )
                gear_payloads.append(payload)
                logger.info(f"Created deployment payload for {serial_number_user_id}")
                
            except Exception as e:
                logger.exception(
                    "Failed to create gear payload for deployment %s. Error: %s",
                    serial_number_user_id,
                    str(e),
                )

        # Process updates (existing gear sets with location changes)
        for serial_number_user_id in to_update:
            edgetech_buoy = serial_number_to_edgetech_buoy[serial_number_user_id]
            edgetech_buoy_lat = edgetech_buoy.currentState.latDeg
            edgetech_buoy_long = edgetech_buoy.currentState.lonDeg

            primary_device_name = f"{serial_number_user_id.replace('/', '_')}_A"
            single_device_name = f"{serial_number_user_id.replace('/', '_')}"
            er_gear = er_gears_devices_id_to_gear.get(primary_device_name) or er_gears_devices_id_to_gear.get(single_device_name)
            
            if not er_gear:
                logger.warning(f"ER gear not found for {serial_number_user_id}, skipping update.")
                continue
            
            # Find device location in ER gear
            er_device_lat = None
            er_device_long = None
            for er_device in er_gear.devices:
                if er_device.mfr_device_id == primary_device_name or er_device.mfr_device_id == single_device_name:
                    er_device_lat = er_device.location.latitude
                    er_device_long = er_device.location.longitude
                    break
            
            if er_device_lat == edgetech_buoy_lat and er_device_long == edgetech_buoy_long:
                # No change in location, skip update
                logger.info(
                    "No change in location for buoy %s, skipping update.",
                    serial_number_user_id,
                )
                continue


            try:
                # Get end unit buoy if this is a two-unit line
                end_unit_buoy = None
                if edgetech_buoy.currentState.isTwoUnitLine:
                    if edgetech_buoy.currentState.endUnit:
                        end_unit_buoy_key = f"{edgetech_buoy.currentState.endUnit}/{get_hashed_user_id(edgetech_buoy.userId)}"
                        end_unit_buoy = serial_number_to_edgetech_buoy.get(end_unit_buoy_key)
                        
                        if not end_unit_buoy:
                            logger.warning(
                                "End unit buoy %s not found for serial number %s, skipping update.",
                                edgetech_buoy.currentState.endUnit,
                                serial_number_user_id,
                            )
                            continue
                    
                    if edgetech_buoy.currentState.startUnit:
                        # This record is for the end unit, skip it
                        continue

                payload = await self._create_gear_payload(
                    buoy=edgetech_buoy,
                    device_status="deployed",
                    manufacturer_id_to_source_id=manufacturer_id_to_source_id,
                    end_unit_buoy=end_unit_buoy,
                    set_id=er_gear.id,
                    include_initial_deployment=False,
                )
                gear_payloads.append(payload)
                logger.info(f"Created update payload for {serial_number_user_id}")
                
            except Exception as e:
                logger.exception(
                    "Failed to create gear payload for update %s. Error: %s",
                    serial_number_user_id,
                    str(e),
                )

        # Process hauls (gear sets explicitly marked as deleted or not deployed)
        # Group devices by gear set to avoid duplicate haul payloads
        haul_gears_processed = set()
        
        for serial_number_user_id in to_haul:
            serial_number, hashed_user_id = serial_number_user_id.split("/", 2)
            primary_device_name = f"{serial_number}_{hashed_user_id}_A"
            single_device_name = f"{serial_number}_{hashed_user_id}"
            
            # Get the EdgeTech buoy data for potential recovery location
            edgetech_buoy = serial_number_to_edgetech_buoy.get(serial_number_user_id)
            
            # Find the corresponding ER gear
            er_gear = er_gears_devices_id_to_gear.get(primary_device_name) or er_gears_devices_id_to_gear.get(single_device_name)
            
            if not er_gear:
                logger.warning(
                    "No ER gear found for buoy %s (tried %s and %s), skipping haul.",
                    serial_number_user_id,
                    primary_device_name,
                    single_device_name,
                )
                continue
            
            # Skip if we already processed this gear set
            if er_gear.display_id in haul_gears_processed:
                logger.info(
                    f"Gear set {er_gear.display_id} already processed for haul, "
                    f"skipping buoy {serial_number_user_id}"
                )
                continue

            try:
                payload = self._create_haul_payload(
                    er_gear=er_gear,
                    edgetech_buoy=edgetech_buoy
                )
                gear_payloads.append(payload)
                haul_gears_processed.add(er_gear.display_id)
                logger.info(
                    f"Created haul payload for gear set {er_gear.display_id} "
                    f"(buoy {serial_number_user_id})"
                )
                
            except Exception as e:
                logger.exception(
                    "Failed to create haul payload for gear set %s (buoy %s). Error: %s",
                    er_gear.display_id if er_gear else "unknown",
                    serial_number_user_id,
                    str(e),
                )

        logger.info(
            "Generated %d gear payload(s):\n%s",
            len(gear_payloads),
            json.dumps(gear_payloads, indent=4, default=str),
        )

        return gear_payloads
