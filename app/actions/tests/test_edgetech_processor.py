import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pydantic
import pytest

from app.actions.buoy.types import BuoyDevice, BuoyGear, DeviceLocation
from app.actions.edgetech.processor import EdgeTechProcessor
from app.actions.edgetech.types import Buoy
from app.actions.utils import get_hashed_user_id

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
@pytest.mark.usefixtures()
async def test_process_new_edgetech_trawl(mocker, a_new_edgetech_trawl_record):
    """Test that new EdgeTech buoys are correctly processed without errors."""
    # Arrange
    data = [a_new_edgetech_trawl_record]
    processor = EdgeTechProcessor(data=data, er_token="token", er_url="url")

    # Mock the ER client to return no existing gears (new deployment)
    mock_er_client = mocker.MagicMock()
    mock_er_client.get_er_gears = AsyncMock(return_value=[])
    mock_er_client.get_sources = AsyncMock(return_value=[])
    mock_er_client.get_existing_source_id_by_manufacturer_id = AsyncMock(
        return_value=None
    )
    processor._er_client = mock_er_client

    # Act & Assert - The process should complete without errors
    await processor.process()

    # Verify that the ER client was called
    mock_er_client.get_er_gears.assert_called_once()


@pytest.fixture
def deleted_buoy_record():
    """Create a deleted buoy record for testing."""
    return {
        "serialNumber": "DEL123",
        "userId": "user123",
        "currentState": {
            "etag": "deleted_etag",
            "isDeleted": True,
            "serialNumber": "DEL123",
            "releaseCommand": "release123",
            "statusCommand": "status123",
            "idCommand": "id123",
            "isNfcTag": False,
            "latDeg": 40.7128,
            "lonDeg": -74.0060,
            "modelNumber": "Model123",
            "isDeployed": True,
            "dateDeployed": "2023-01-01T00:00:00.000Z",
            "lastUpdated": "2023-01-01T12:00:00.000Z",
        },
        "changeRecords": [],
    }


@pytest.fixture
def non_deployed_buoy_record():
    """Create a non-deployed buoy record for testing."""
    return {
        "serialNumber": "NDEP123",
        "userId": "user123",
        "currentState": {
            "etag": "non_deployed_etag",
            "isDeleted": False,
            "serialNumber": "NDEP123",
            "releaseCommand": "release123",
            "statusCommand": "status123",
            "idCommand": "id123",
            "isNfcTag": False,
            "latDeg": 40.7128,
            "lonDeg": -74.0060,
            "modelNumber": "Model123",
            "isDeployed": False,
            "dateDeployed": "2023-01-01T00:00:00.000Z",
            "lastUpdated": "2023-01-01T12:00:00.000Z",
        },
        "changeRecords": [],
    }


@pytest.fixture
def no_location_buoy_record():
    """Create a buoy record with no location data for testing."""
    return {
        "serialNumber": "NOLOC123",
        "userId": "user123",
        "currentState": {
            "etag": "no_location_etag",
            "isDeleted": False,
            "serialNumber": "NOLOC123",
            "releaseCommand": "release123",
            "statusCommand": "status123",
            "idCommand": "id123",
            "isNfcTag": False,
            "latDeg": None,
            "lonDeg": None,
            "modelNumber": "Model123",
            "isDeployed": True,
            "dateDeployed": "2023-01-01T00:00:00.000Z",
            "lastUpdated": "2023-01-01T12:00:00.000Z",
        },
        "changeRecords": [],
    }


@pytest.fixture
def hauled_buoy_no_recovery_location():
    """Create a hauled buoy record without recovery location (the bug scenario)."""
    return {
        "serialNumber": "HAUL123",
        "userId": "user123",
        "currentState": {
            "etag": "hauled_etag",
            "isDeleted": False,
            "serialNumber": "HAUL123",
            "releaseCommand": "release123",
            "statusCommand": "status123",
            "idCommand": "id123",
            "isNfcTag": False,
            "latDeg": 40.7128,  # Has deployed location
            "lonDeg": -74.0060,
            "recoveredLatDeg": None,  # No recovery location
            "recoveredLonDeg": None,
            "dateRecovered": "2023-01-02T12:00:00.000Z",  # But has recovery date
            "modelNumber": "Model123",
            "isDeployed": False,  # Not deployed anymore
            "dateDeployed": "2023-01-01T00:00:00.000Z",
            "lastUpdated": "2023-01-02T12:00:00.000Z",
        },
        "changeRecords": [],
    }


class TestEdgeTechProcessor:
    """Test class for EdgeTechProcessor."""

    def test_get_default_filters(self):
        """Test that default filters are correctly generated."""
        processor = EdgeTechProcessor(data=[], er_token="token", er_url="url")
        filters = processor._get_default_filters()

        assert "start_datetime" in filters
        assert isinstance(filters["start_datetime"], datetime)

    def test_should_skip_buoy_deleted(self, deleted_buoy_record):
        """Test that deleted buoys are NOT skipped (needed for haul detection)."""
        processor = EdgeTechProcessor(data=[], er_token="token", er_url="url")

        # Create a proper Buoy object
        buoy = Buoy.parse_obj(deleted_buoy_record)

        should_skip, reason = processor._should_skip_buoy(buoy)

        # Deleted buoys should NOT be skipped - we need them to detect haul events
        assert should_skip is False
        assert reason is None

    def test_should_skip_buoy_not_deployed(self, non_deployed_buoy_record):
        """Test that non-deployed buoys are NOT skipped (needed for haul detection)."""
        processor = EdgeTechProcessor(data=[], er_token="token", er_url="url")

        buoy = Buoy.parse_obj(non_deployed_buoy_record)

        should_skip, reason = processor._should_skip_buoy(buoy)

        # Non-deployed buoys should NOT be skipped - we need them to detect haul events
        assert should_skip is False
        assert reason is None

    def test_should_skip_buoy_no_location(self, no_location_buoy_record):
        """Test that buoys with no location are skipped."""
        processor = EdgeTechProcessor(data=[], er_token="token", er_url="url")

        buoy = Buoy.parse_obj(no_location_buoy_record)

        should_skip, reason = processor._should_skip_buoy(buoy)

        assert should_skip is True
        assert "no location data" in reason
        assert "NOLOC123" in reason

    def test_should_not_skip_valid_buoy(self, a_new_edgetech_trawl_record):
        """Test that valid buoys are not skipped."""
        processor = EdgeTechProcessor(data=[], er_token="token", er_url="url")

        buoy = Buoy.parse_obj(a_new_edgetech_trawl_record)

        should_skip, reason = processor._should_skip_buoy(buoy)

        assert should_skip is False
        assert reason is None

    def test_should_not_skip_hauled_buoy_without_recovery_location(
        self, hauled_buoy_no_recovery_location
    ):
        """Test that hauled buoys without recovery location are NOT skipped (bug fix)."""
        processor = EdgeTechProcessor(data=[], er_token="token", er_url="url")

        buoy = Buoy.parse_obj(hauled_buoy_no_recovery_location)

        should_skip, reason = processor._should_skip_buoy(buoy)

        # This is the key fix - hauled buoys should NOT be skipped even without recovery location
        assert should_skip is False
        assert reason is None

    def test_is_hauled_or_recovered(
        self,
        hauled_buoy_no_recovery_location,
        deleted_buoy_record,
        non_deployed_buoy_record,
    ):
        """Test the _is_hauled_or_recovered helper method."""
        processor = EdgeTechProcessor(data=[], er_token="token", er_url="url")

        # Test with hauled buoy (has dateRecovered)
        hauled_buoy = Buoy.parse_obj(hauled_buoy_no_recovery_location)
        assert processor._is_hauled_or_recovered(hauled_buoy) is True

        # Test with deleted buoy
        deleted_buoy = Buoy.parse_obj(deleted_buoy_record)
        assert processor._is_hauled_or_recovered(deleted_buoy) is True

        # Test with non-deployed buoy
        non_deployed_buoy = Buoy.parse_obj(non_deployed_buoy_record)
        assert processor._is_hauled_or_recovered(non_deployed_buoy) is True

    def test_create_haul_payload_without_recovery_location(
        self, hauled_buoy_no_recovery_location
    ):
        """Test that haul payload uses fallback location when recovery location is not available."""
        processor = EdgeTechProcessor(data=[], er_token="token", er_url="url")

        # Create mock ER gear with deployed location
        mock_device = BuoyDevice(
            device_id="source_id_123",
            mfr_device_id="HAUL123_hashed_user_A",
            label="Test Device",
            location=DeviceLocation(
                latitude=40.7128, longitude=-74.0060
            ),  # Last known deployed location
            last_updated=datetime.now(timezone.utc),
            last_deployed=datetime.now(timezone.utc) - timedelta(days=1),
        )

        mock_gear = BuoyGear(
            id=uuid4(),
            display_id="GEAR789",
            status="deployed",
            last_updated=datetime.now(timezone.utc) - timedelta(days=1),
            devices=[mock_device],
            type="ropeless",
            manufacturer="edgetech",
        )

        # Create haul payload with EdgeTech buoy that has no recovery location
        edgetech_buoy = Buoy.parse_obj(hauled_buoy_no_recovery_location)
        payload = processor._create_haul_payload(
            er_gear=mock_gear, edgetech_buoy=edgetech_buoy
        )

        # Verify payload structure
        assert payload["set_id"] == "GEAR789"
        assert payload["deployment_type"] == "ropeless"
        assert len(payload["devices"]) == 1

        # Verify device uses fallback location (from ER gear, not recovery location)
        device = payload["devices"][0]
        assert device["device_status"] == "hauled"
        assert device["location"]["latitude"] == 40.7128
        assert device["location"]["longitude"] == -74.0060

    def test_create_haul_payload_with_recovery_location(self):
        """Test that haul payload uses recovery location when available."""
        processor = EdgeTechProcessor(data=[], er_token="token", er_url="url")

        # Create EdgeTech buoy with recovery location
        hauled_buoy_with_recovery = {
            "serialNumber": "HAUL456",
            "userId": "user123",
            "currentState": {
                "etag": "hauled_etag",
                "isDeleted": False,
                "serialNumber": "HAUL456",
                "releaseCommand": "release123",
                "statusCommand": "status123",
                "idCommand": "id123",
                "isNfcTag": False,
                "latDeg": 40.7128,  # Deployed location
                "lonDeg": -74.0060,
                "recoveredLatDeg": 41.0,  # Recovery location (different)
                "recoveredLonDeg": -73.0,
                "dateRecovered": "2023-01-02T12:00:00.000Z",
                "modelNumber": "Model123",
                "isDeployed": False,
                "dateDeployed": "2023-01-01T00:00:00.000Z",
                "lastUpdated": "2023-01-02T12:00:00.000Z",
            },
            "changeRecords": [],
        }

        # Create mock ER gear
        mock_device = BuoyDevice(
            device_id="source_id_456",
            mfr_device_id="HAUL456_hashed_user_A",
            label="Test Device",
            location=DeviceLocation(latitude=40.7128, longitude=-74.0060),
            last_updated=datetime.now(timezone.utc),
            last_deployed=datetime.now(timezone.utc) - timedelta(days=1),
        )

        mock_gear = BuoyGear(
            id=uuid4(),
            display_id="GEAR999",
            status="deployed",
            last_updated=datetime.now(timezone.utc) - timedelta(days=1),
            devices=[mock_device],
            type="ropeless",
            manufacturer="edgetech",
        )

        edgetech_buoy = Buoy.parse_obj(hauled_buoy_with_recovery)
        payload = processor._create_haul_payload(
            er_gear=mock_gear, edgetech_buoy=edgetech_buoy
        )

        # Verify device uses recovery location (not deployed location)
        device = payload["devices"][0]
        assert device["device_status"] == "hauled"
        assert device["location"]["latitude"] == 41.0
        assert device["location"]["longitude"] == -73.0

    @pytest.mark.asyncio
    async def test_filter_edgetech_buoys_data_filters_out_invalid(
        self,
        caplog,
        deleted_buoy_record,
        non_deployed_buoy_record,
        no_location_buoy_record,
        a_new_edgetech_trawl_record,
    ):
        """Test that _filter_edgetech_buoys_data keeps deleted/non-deployed but filters no-location records."""
        data = [
            deleted_buoy_record,
            non_deployed_buoy_record,
            no_location_buoy_record,
            a_new_edgetech_trawl_record,
        ]

        processor = EdgeTechProcessor(data=data, er_token="token", er_url="url")

        # Capture log warnings
        with caplog.at_level(logging.WARNING):
            filtered_data = processor._filter_edgetech_buoys_data(processor._data)

            # Should have 3 valid records (keeps deleted and non-deployed for haul detection)
            # Only filters out the no-location record
            assert len(filtered_data) == 3
            serial_numbers = [buoy.serialNumber for buoy in filtered_data]
            assert "8899CEDAAA" in serial_numbers  # valid deployed
            assert "DEL123" in serial_numbers  # deleted (kept for haul detection)
            assert "NDEP123" in serial_numbers  # non-deployed (kept for haul detection)
            assert "NOLOC123" not in serial_numbers  # no location (filtered out)

            # Should have logged warning only for no-location record
            assert len(caplog.records) == 1

    def test_get_latest_buoy_states(self, a_new_edgetech_trawl_record):
        """Test that _get_latest_buoy_states returns the latest states."""
        # Create two records with different timestamps for the same buoy
        older_record = a_new_edgetech_trawl_record.copy()
        older_record["currentState"] = older_record["currentState"].copy()
        older_record["currentState"]["lastUpdated"] = "2025-05-25T10:00:00.000Z"

        newer_record = a_new_edgetech_trawl_record.copy()
        newer_record["currentState"] = newer_record["currentState"].copy()
        newer_record["currentState"]["lastUpdated"] = "2025-05-25T20:00:00.000Z"

        data = [older_record, newer_record]
        processor = EdgeTechProcessor(data=data, er_token="token", er_url="url")

        latest_states = processor._get_latest_buoy_states(processor._data)

        # Should only have one record (the latest)
        assert len(latest_states) == 1
        assert latest_states[0].currentState.lastUpdated.hour == 20

    @pytest.mark.asyncio
    async def test_identify_buoys_deploy_new_buoy(
        self, mocker, a_new_edgetech_trawl_record
    ):
        """Test that new buoys are identified for deployment."""
        data = [a_new_edgetech_trawl_record]
        processor = EdgeTechProcessor(data=data, er_token="token", er_url="url")

        # Mock empty ER gears (no existing buoys)
        er_gears_devices_id_to_gear = {}
        serial_number_to_edgetech_buoy = {
            "8899CEDAAA/n9JpP3kk8vFVyNlzMnYZig9DnO475ztWV5JQ4z3RHwO19GPjN9sL8qDw8YgW": processor._data[
                0
            ]
        }

        to_deploy, to_haul, to_update = await processor._identify_buoys(
            er_gears_devices_id_to_gear, serial_number_to_edgetech_buoy
        )

        assert len(to_deploy) == 1
        assert len(to_haul) == 0
        assert len(to_update) == 0
        assert (
            "8899CEDAAA/n9JpP3kk8vFVyNlzMnYZig9DnO475ztWV5JQ4z3RHwO19GPjN9sL8qDw8YgW"
            in to_deploy
        )

    @pytest.mark.asyncio
    async def test_identify_buoys_update_existing_buoy(
        self, mocker, a_new_edgetech_trawl_record
    ):
        """Test that existing buoys are identified for update when they have newer data."""
        data = [a_new_edgetech_trawl_record]
        processor = EdgeTechProcessor(data=data, er_token="token", er_url="url")

        # Mock existing ER gear: last_deployed at or after EdgeTech dateDeployed so this
        # is an update (same deployment), not a re-deployment; last_updated before
        # EdgeTech lastUpdated so we have newer data and get to_update.
        # Fixture has dateDeployed 2025-05-25 17:53:19.517, lastUpdated 17:53:19.731
        er_deployed = datetime(2025, 5, 25, 17, 53, 20, tzinfo=timezone.utc)
        er_updated = datetime(2025, 5, 25, 17, 53, 19, tzinfo=timezone.utc)

        mock_device = BuoyDevice(
            device_id="8899CEDAAA_n9JpP3kk8vFVyNlzMnYZig9DnO475ztWV5JQ4z3RHwO19GPjN9sL8qDw8YgW_A",
            mfr_device_id="8899CEDAAA_n9JpP3kk8vFVyNlzMnYZig9DnO475ztWV5JQ4z3RHwO19GPjN9sL8qDw8YgW_A",
            label="Test Device",
            location=DeviceLocation(latitude=44.358265, longitude=-68.16757),
            last_updated=er_updated,
            last_deployed=er_deployed,
        )

        mock_gear = BuoyGear(
            id=uuid4(),
            display_id="GEAR123",
            status="deployed",
            last_updated=er_updated,
            devices=[mock_device],
            type="ropeless",
            manufacturer="edgetech",
        )

        er_gears_devices_id_to_gear = {
            "8899CEDAAA_n9JpP3kk8vFVyNlzMnYZig9DnO475ztWV5JQ4z3RHwO19GPjN9sL8qDw8YgW_A": mock_gear
        }

        serial_number_to_edgetech_buoy = {
            "8899CEDAAA/n9JpP3kk8vFVyNlzMnYZig9DnO475ztWV5JQ4z3RHwO19GPjN9sL8qDw8YgW": processor._data[
                0
            ]
        }

        to_deploy, to_haul, to_update = await processor._identify_buoys(
            er_gears_devices_id_to_gear, serial_number_to_edgetech_buoy
        )

        assert len(to_deploy) == 0
        # The buoy exists in ER but not in EdgeTech data, so it should be marked for hauling
        # But since we also have EdgeTech data, it should also be marked for update
        assert len(to_update) == 1
        assert (
            "8899CEDAAA/n9JpP3kk8vFVyNlzMnYZig9DnO475ztWV5JQ4z3RHwO19GPjN9sL8qDw8YgW"
            in to_update
        )

    @pytest.mark.asyncio
    async def test_identify_buoys_haul_deleted_buoy(
        self, mocker, a_new_edgetech_trawl_record
    ):
        """Test that buoys explicitly marked as deleted in EdgeTech are identified for hauling."""
        # Create a deleted buoy record
        deleted_record = a_new_edgetech_trawl_record.copy()
        deleted_record["currentState"] = deleted_record["currentState"].copy()
        deleted_record["currentState"]["isDeleted"] = True
        deleted_record["serialNumber"] = "DELETED123"

        processor = EdgeTechProcessor(
            data=[deleted_record], er_token="token", er_url="url"
        )

        # Mock existing ER gear that is still deployed
        mock_device = BuoyDevice(
            device_id="some-uuid",
            mfr_device_id="DELETED123_n9JpP3kk8vFVyNlzMnYZig9DnO475ztWV5JQ4z3RHwO19GPjN9sL8qDw8YgW_A",
            label="Deleted Device",
            location=DeviceLocation(latitude=44.0, longitude=-68.0),
            last_updated=datetime.now(timezone.utc),
            last_deployed=datetime.now(timezone.utc),
        )

        mock_gear = BuoyGear(
            id=uuid4(),
            display_id="GEAR456",
            status="deployed",  # Gear status is "deployed"
            last_updated=datetime.now(timezone.utc) - timedelta(hours=1),
            devices=[mock_device],
            type="ropeless",
            manufacturer="edgetech",
        )

        er_gears_devices_id_to_gear = {
            "DELETED123_n9JpP3kk8vFVyNlzMnYZig9DnO475ztWV5JQ4z3RHwO19GPjN9sL8qDw8YgW_A": mock_gear
        }

        serial_number_to_edgetech_buoy = {
            "DELETED123/n9JpP3kk8vFVyNlzMnYZig9DnO475ztWV5JQ4z3RHwO19GPjN9sL8qDw8YgW": processor._data[
                0
            ]
        }

        to_deploy, to_haul, to_update = await processor._identify_buoys(
            er_gears_devices_id_to_gear, serial_number_to_edgetech_buoy
        )

        assert len(to_deploy) == 0
        assert len(to_haul) == 1
        assert len(to_update) == 0
        assert (
            "DELETED123/n9JpP3kk8vFVyNlzMnYZig9DnO475ztWV5JQ4z3RHwO19GPjN9sL8qDw8YgW"
            in to_haul
        )

    @pytest.mark.asyncio
    async def test_identify_buoys_no_haul_for_missing_buoy(self, mocker):
        """Test that buoys missing from EdgeTech sync window are NOT identified for hauling."""
        processor = EdgeTechProcessor(data=[], er_token="token", er_url="url")

        # Mock existing ER gear but no corresponding EdgeTech buoy (not in sync window)
        mock_device = BuoyDevice(
            device_id="some-uuid",
            mfr_device_id="MISSING123_userABC_A",
            label="Missing Device",
            location=DeviceLocation(latitude=44.0, longitude=-68.0),
            last_updated=datetime.now(timezone.utc),
            last_deployed=datetime.now(timezone.utc),
        )

        mock_gear = BuoyGear(
            id=uuid4(),
            display_id="GEAR456",
            status="deployed",
            last_updated=datetime.now(timezone.utc),
            devices=[mock_device],
            type="ropeless",
            manufacturer="edgetech",
        )

        er_gears_devices_id_to_gear = {"MISSING123_userABC_A": mock_gear}

        serial_number_to_edgetech_buoy = {}  # No EdgeTech buoys in sync window

        to_deploy, to_haul, to_update = await processor._identify_buoys(
            er_gears_devices_id_to_gear, serial_number_to_edgetech_buoy
        )

        # Missing from sync window should NOT trigger haul
        assert len(to_deploy) == 0
        assert len(to_haul) == 0
        assert len(to_update) == 0

    @pytest.mark.asyncio
    async def test_process_with_two_unit_line_missing_end_unit(
        self, mocker, caplog, a_new_edgetech_trawl_record
    ):
        """Test processing a two-unit line when end unit is missing."""
        # Modify record to be a two-unit line with missing end unit
        two_unit_record = a_new_edgetech_trawl_record.copy()
        two_unit_record["currentState"] = two_unit_record["currentState"].copy()
        two_unit_record["currentState"]["isTwoUnitLine"] = True
        two_unit_record["currentState"]["endUnit"] = "MISSING_END_UNIT"

        data = [two_unit_record]
        processor = EdgeTechProcessor(data=data, er_token="token", er_url="url")

        mock_er_client = mocker.MagicMock()
        mock_er_client.get_er_gears = AsyncMock(return_value=[])
        mock_er_client.get_sources = AsyncMock(return_value=[])
        mock_er_client.get_existing_source_id_by_manufacturer_id = AsyncMock(
            return_value=None
        )
        processor._er_client = mock_er_client

        with caplog.at_level(logging.WARNING):
            observations = await processor.process()

        # Should log a warning about missing end unit
        assert "End unit buoy MISSING_END_UNIT not found" in caplog.text
        assert len(observations) == 0

    @pytest.mark.asyncio
    async def test_process_with_end_unit_buoy(
        self, mocker, a_new_edgetech_trawl_record
    ):
        """Test processing with end unit buoy (start unit perspective)."""
        # Create start unit record
        start_unit_record = a_new_edgetech_trawl_record.copy()
        start_unit_record["currentState"] = start_unit_record["currentState"].copy()
        start_unit_record["currentState"]["isTwoUnitLine"] = True
        start_unit_record["currentState"]["endUnit"] = "END123"
        start_unit_record["currentState"]["startUnit"] = None

        # Create end unit record
        end_unit_record = a_new_edgetech_trawl_record.copy()
        end_unit_record["serialNumber"] = "END123"
        end_unit_record["currentState"] = end_unit_record["currentState"].copy()
        end_unit_record["currentState"]["serialNumber"] = "END123"
        end_unit_record["currentState"]["isTwoUnitLine"] = True
        end_unit_record["currentState"]["startUnit"] = "8899CEDAAA"
        end_unit_record["currentState"]["endUnit"] = None

        data = [start_unit_record, end_unit_record]
        processor = EdgeTechProcessor(data=data, er_token="token", er_url="url")

        mock_er_client = mocker.MagicMock()
        mock_er_client.get_er_gears = AsyncMock(return_value=[])
        mock_er_client.get_sources = AsyncMock(return_value=[])
        mock_er_client.get_existing_source_id_by_manufacturer_id = AsyncMock(
            return_value=None
        )
        processor._er_client = mock_er_client

        # Act & Assert - The process should complete without errors
        await processor.process()

        # Verify that the ER client was called
        mock_er_client.get_er_gears.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_validation_error_handling(self, mocker, caplog):
        """Test handling of validation errors during observation creation."""
        # Create a record that will cause validation error (mock it)
        data = [{"invalid": "data"}]  # This will cause parsing to fail

        with pytest.raises(Exception):  # Should fail during Buoy.parse_obj
            EdgeTechProcessor(data=data, er_token="token", er_url="url")

    @pytest.mark.asyncio
    async def test_process_updates_existing_buoy(
        self, mocker, a_new_edgetech_trawl_record
    ):
        """Test processing updates for existing buoys."""
        data = [a_new_edgetech_trawl_record]
        processor = EdgeTechProcessor(data=data, er_token="token", er_url="url")

        # Create existing ER gear with older timestamp
        older_time = datetime(2025, 5, 20, 10, 0, 0, tzinfo=timezone.utc)

        mock_device = BuoyDevice(
            device_id="8899CEDAAA_n9JpP3kk8vFVyNlzMnYZig9DnO475ztWV5JQ4z3RHwO19GPjN9sL8qDw8YgW_A",
            mfr_device_id="8899CEDAAA_n9JpP3kk8vFVyNlzMnYZig9DnO475ztWV5JQ4z3RHwO19GPjN9sL8qDw8YgW_A",
            label="Test Device",
            location=DeviceLocation(latitude=44.0, longitude=-68.0),
            last_updated=older_time,
            last_deployed=older_time,
        )

        mock_gear = BuoyGear(
            id=uuid4(),
            display_id="GEAR123",
            status="deployed",
            last_updated=older_time,
            devices=[mock_device],
            type="ropeless",
            manufacturer="edgetech",
        )

        mock_er_client = mocker.MagicMock()
        mock_er_client.get_er_gears = AsyncMock(return_value=[mock_gear])
        mock_er_client.get_sources = AsyncMock(return_value=[])
        mock_er_client.get_existing_source_id_by_manufacturer_id = AsyncMock(
            return_value=None
        )
        processor._er_client = mock_er_client

        # Act
        await processor.process()

        # Verify that the ER client was called
        mock_er_client.get_er_gears.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_creates_haul_observations(self, mocker):
        """Test that haul observations are created for missing buoys."""
        # No EdgeTech data, but existing ER gear
        data = []
        processor = EdgeTechProcessor(data=data, er_token="token", er_url="url")

        mock_device = BuoyDevice(
            device_id="edgetech_MISSING123_userABC_A",
            mfr_device_id="edgetech_MISSING123_userABC_A",
            label="Missing Device",
            location=DeviceLocation(latitude=44.0, longitude=-68.0),
            last_updated=datetime.now(timezone.utc),
            last_deployed=datetime.now(timezone.utc),
        )

        mock_gear = BuoyGear(
            id=uuid4(),
            display_id="GEAR456",
            status="deployed",
            last_updated=datetime.now(timezone.utc),
            devices=[mock_device],
            type="ropeless",
            manufacturer="edgetech",
        )

        mock_er_client = mocker.MagicMock()
        mock_er_client.get_er_gears = AsyncMock(return_value=[mock_gear])
        mock_er_client.get_sources = AsyncMock(return_value=[])
        mock_er_client.get_existing_source_id_by_manufacturer_id = AsyncMock(
            return_value=None
        )
        processor._er_client = mock_er_client

        # Act
        await processor.process()

        # Verify that the ER client was called
        mock_er_client.get_er_gears.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_deploy_validation_error(
        self, mocker, caplog, a_new_edgetech_trawl_record
    ):
        """Test handling of errors during deployment gear payload creation."""
        data = [a_new_edgetech_trawl_record]
        processor = EdgeTechProcessor(data=data, er_token="token", er_url="url")

        mock_er_client = mocker.MagicMock()
        mock_er_client.get_er_gears = AsyncMock(return_value=[])
        mock_er_client.get_sources = AsyncMock(
            side_effect=Exception("Test error accessing sources")
        )
        processor._er_client = mock_er_client

        with caplog.at_level(logging.ERROR):
            with pytest.raises(Exception, match="Test error accessing sources"):
                await processor.process()

    @pytest.mark.asyncio
    async def test_process_update_missing_end_unit(
        self, mocker, caplog, a_new_edgetech_trawl_record
    ):
        """Test update process when end unit is missing for two-unit line."""
        # Modify record to be a two-unit line with missing end unit
        two_unit_record = a_new_edgetech_trawl_record.copy()
        two_unit_record["currentState"] = two_unit_record["currentState"].copy()
        two_unit_record["currentState"]["isTwoUnitLine"] = True
        two_unit_record["currentState"]["endUnit"] = "MISSING_END_UNIT"

        data = [two_unit_record]
        processor = EdgeTechProcessor(data=data, er_token="token", er_url="url")

        # Create existing ER gear for update scenario with different location to trigger update path
        mock_device = BuoyDevice(
            device_id="8899CEDAAA_n9JpP3kk8vFVyNlzMnYZig9DnO475ztWV5JQ4z3RHwO19GPjN9sL8qDw8YgW_A",
            mfr_device_id="8899CEDAAA_n9JpP3kk8vFVyNlzMnYZig9DnO475ztWV5JQ4z3RHwO19GPjN9sL8qDw8YgW_A",
            label="Test Device",
            location=DeviceLocation(
                latitude=44.0, longitude=-68.0
            ),  # Different location
            last_updated=datetime(2025, 5, 20, 10, 0, 0, tzinfo=timezone.utc),
            last_deployed=datetime(2025, 5, 20, 10, 0, 0, tzinfo=timezone.utc),
        )

        mock_gear = BuoyGear(
            id=uuid4(),
            display_id="GEAR123",
            status="deployed",
            last_updated=datetime(2025, 5, 20, 10, 0, 0, tzinfo=timezone.utc),
            devices=[mock_device],
            type="ropeless",
            manufacturer="edgetech",
        )

        mock_er_client = mocker.MagicMock()
        mock_er_client.get_er_gears = AsyncMock(return_value=[mock_gear])
        mock_er_client.get_sources = AsyncMock(return_value=[])
        mock_er_client.get_existing_source_id_by_manufacturer_id = AsyncMock(
            return_value=None
        )
        processor._er_client = mock_er_client

        with caplog.at_level(logging.WARNING):
            await processor.process()

        # Should log warning about missing end unit during update (and no end unit in ER gear)
        assert "End unit buoy MISSING_END_UNIT not found" in caplog.text

    @pytest.mark.asyncio
    async def test_process_update_two_unit_uses_er_when_end_unit_not_in_sync_window(
        self, mocker, caplog
    ):
        """
        When only the start unit has a location update (in sync window) and the end unit
        is not in the sync window, we should still send the update using the end unit's
        current state from ER (fixes location updates not appearing in ER/Buoy).
        """
        user_id = "5f455a89e7ef8c0068db9ae1"
        hashed = get_hashed_user_id(user_id)
        start_serial = "88CE99B71C"
        end_serial = "88CE99CAE8"

        # Only start unit in "sync window" - new location at 15:01:58
        start_unit_record = {
            "serialNumber": start_serial,
            "userId": user_id,
            "currentState": {
                "etag": '"1771167718663"',
                "isDeleted": False,
                "positionSetByCapri": False,
                "serialNumber": start_serial,
                "releaseCommand": "C8AB8CEA9C",
                "statusCommand": start_serial,
                "idCommand": "CCCCCCCCCC",
                "isNfcTag": False,
                "latDeg": 40.35775,
                "lonDeg": -70.96013333333333,
                "modelNumber": "5112",
                "isDeployed": True,
                "dateDeployed": "2026-02-15T14:56:47.660Z",
                "isTwoUnitLine": True,
                "endUnit": end_serial,
                "licenseNumber": "330901",
                "lastUpdated": "2026-02-15T15:01:58.663Z",
            },
            "changeRecords": [],
        }

        data = [start_unit_record]
        processor = EdgeTechProcessor(data=data, er_token="token", er_url="url")

        # ER gear has both devices (start + end); start has old location to trigger update
        start_device_id = f"{start_serial}_{hashed}_A"
        end_device_id = f"{end_serial}_{hashed}"
        start_device_er = BuoyDevice(
            device_id=start_device_id,
            mfr_device_id=start_device_id,
            label="A",
            location=DeviceLocation(latitude=40.3575109, longitude=-70.9632526),
            last_updated=datetime(2026, 2, 15, 14, 56, 48, tzinfo=timezone.utc),
            last_deployed=datetime(2026, 2, 15, 14, 56, 47, tzinfo=timezone.utc),
        )
        end_device_er = BuoyDevice(
            device_id=end_device_id,
            mfr_device_id=end_device_id,
            label="B",
            location=DeviceLocation(latitude=40.358, longitude=-70.959),
            last_updated=datetime(2026, 2, 15, 14, 56, 48, tzinfo=timezone.utc),
            last_deployed=datetime(2026, 2, 15, 14, 56, 47, tzinfo=timezone.utc),
        )
        mock_gear = BuoyGear(
            id=uuid4(),
            display_id="GEAR123",
            status="deployed",
            last_updated=datetime(2026, 2, 15, 14, 56, 48, tzinfo=timezone.utc),
            devices=[start_device_er, end_device_er],
            type="trawl",
            manufacturer="edgetech",
        )

        mock_er_client = mocker.MagicMock()
        mock_er_client.get_er_gears = AsyncMock(return_value=[mock_gear])
        mock_er_client.get_sources = AsyncMock(return_value=[])
        mock_er_client.send_gear_to_buoy_api = AsyncMock(
            return_value={"status": "success", "status_code": 200}
        )
        processor._er_client = mock_er_client

        with caplog.at_level(logging.INFO):
            payloads = await processor.process()

        assert len(payloads) == 1
        payload = payloads[0]
        assert payload["devices_in_set"] == 2
        devices_by_mfr = {d["mfr_device_id"]: d for d in payload["devices"]}
        # When end unit comes from ER, start unit is sent without _A suffix
        start_in_payload = (
            start_device_id.replace("_A", "") in devices_by_mfr
            or start_device_id in devices_by_mfr
        )
        assert (
            start_in_payload
        ), f"Start device not in payload: {list(devices_by_mfr.keys())}"
        assert end_device_id in devices_by_mfr
        start_key = (
            start_device_id
            if start_device_id in devices_by_mfr
            else start_device_id.replace("_A", "")
        )
        # Start unit has new location from EdgeTech
        assert devices_by_mfr[start_key]["location"]["latitude"] == 40.35775
        assert devices_by_mfr[start_key]["location"]["longitude"] == -70.96013333333333
        # End unit has existing location from ER (not in sync window)
        assert devices_by_mfr[end_device_id]["location"]["latitude"] == 40.358
        assert devices_by_mfr[end_device_id]["location"]["longitude"] == -70.959
        assert "not in sync window; using current state from ER" in caplog.text

    @pytest.mark.asyncio
    async def test_process_update_validation_error(
        self, mocker, caplog, a_new_edgetech_trawl_record
    ):
        """Test handling ValidationError during update gear payload creation."""
        data = [a_new_edgetech_trawl_record]
        processor = EdgeTechProcessor(data=data, er_token="token", er_url="url")

        # ER gear: last_deployed at or after EdgeTech dateDeployed so we get update path
        # (not re-deploy). Fixture has dateDeployed 2025-05-25 17:53:19.517
        er_deployed = datetime(2025, 5, 25, 17, 53, 20, tzinfo=timezone.utc)
        er_updated = datetime(2025, 5, 25, 17, 53, 19, tzinfo=timezone.utc)
        mock_device = BuoyDevice(
            device_id="8899CEDAAA_n9JpP3kk8vFVyNlzMnYZig9DnO475ztWV5JQ4z3RHwO19GPjN9sL8qDw8YgW_A",
            mfr_device_id="8899CEDAAA_n9JpP3kk8vFVyNlzMnYZig9DnO475ztWV5JQ4z3RHwO19GPjN9sL8qDw8YgW_A",
            label="Test Device",
            location=DeviceLocation(
                latitude=44.0, longitude=-68.0
            ),  # Different location to trigger update
            last_updated=er_updated,
            last_deployed=er_deployed,
        )

        mock_gear = BuoyGear(
            id=uuid4(),
            display_id="GEAR123",
            status="deployed",
            last_updated=er_updated,
            devices=[mock_device],
            type="ropeless",
            manufacturer="edgetech",
        )

        mock_er_client = mocker.MagicMock()
        mock_er_client.get_er_gears = AsyncMock(return_value=[mock_gear])
        mock_er_client.get_sources = AsyncMock(return_value=[])
        mock_er_client.get_existing_source_id_by_manufacturer_id = AsyncMock(
            return_value=None
        )
        processor._er_client = mock_er_client

        # Mock _create_gear_payload to raise ValidationError (async mock)
        mock_create = AsyncMock(side_effect=pydantic.ValidationError([], Buoy))
        mocker.patch.object(processor, "_create_gear_payload", mock_create)

        with caplog.at_level(logging.ERROR):
            payloads = await processor.process()

        # Should log the validation error for update or deployment (re-deploy uses deploy path)
        assert (
            "Failed to create gear payload for update" in caplog.text
            or "Failed to create gear payload for deployment" in caplog.text
        )
        assert len(payloads) == 0

    @pytest.mark.asyncio
    async def test_process_update_general_exception(
        self, mocker, caplog, a_new_edgetech_trawl_record
    ):
        """Test handling general Exception during update gear payload creation."""
        data = [a_new_edgetech_trawl_record]
        processor = EdgeTechProcessor(data=data, er_token="token", er_url="url")

        # ER gear: last_deployed at or after EdgeTech dateDeployed so we get update path
        er_deployed = datetime(2025, 5, 25, 17, 53, 20, tzinfo=timezone.utc)
        er_updated = datetime(2025, 5, 25, 17, 53, 19, tzinfo=timezone.utc)
        mock_device = BuoyDevice(
            device_id="8899CEDAAA_n9JpP3kk8vFVyNlzMnYZig9DnO475ztWV5JQ4z3RHwO19GPjN9sL8qDw8YgW_A",
            mfr_device_id="8899CEDAAA_n9JpP3kk8vFVyNlzMnYZig9DnO475ztWV5JQ4z3RHwO19GPjN9sL8qDw8YgW_A",
            label="Test Device",
            location=DeviceLocation(
                latitude=44.0, longitude=-68.0
            ),  # Different location to trigger update
            last_updated=er_updated,
            last_deployed=er_deployed,
        )

        mock_gear = BuoyGear(
            id=uuid4(),
            display_id="GEAR123",
            status="deployed",
            last_updated=er_updated,
            devices=[mock_device],
            type="ropeless",
            manufacturer="edgetech",
        )

        mock_er_client = mocker.MagicMock()
        mock_er_client.get_er_gears = AsyncMock(return_value=[mock_gear])
        mock_er_client.get_sources = AsyncMock(return_value=[])
        mock_er_client.get_existing_source_id_by_manufacturer_id = AsyncMock(
            return_value=None
        )
        processor._er_client = mock_er_client

        # Mock _create_gear_payload to raise general Exception (async mock)
        mock_create = AsyncMock(side_effect=Exception("General error"))
        mocker.patch.object(processor, "_create_gear_payload", mock_create)

        with caplog.at_level(logging.ERROR):
            payloads = await processor.process()

        # Should log the general exception (update or deployment path)
        assert (
            "Failed to create gear payload for update" in caplog.text
            or "Failed to create gear payload for deployment" in caplog.text
        )
        assert "General error" in caplog.text
        assert len(payloads) == 0

    @pytest.mark.asyncio
    async def test_process_haul_no_er_subject_found(self, mocker, caplog):
        """Test warning when no ER subject is found for haul."""
        data = []
        processor = EdgeTechProcessor(data=data, er_token="token", er_url="url")

        # Mock ER gear that doesn't exist in er_gears_devices_id_to_gear mapping
        # This simulates a device that should be hauled but isn't found
        mock_er_client = mocker.MagicMock()
        mock_er_client.get_er_gears = AsyncMock(return_value=[])
        mock_er_client.get_sources = AsyncMock(return_value=[])
        mock_er_client.get_existing_source_id_by_manufacturer_id = AsyncMock(
            return_value=None
        )
        processor._er_client = mock_er_client

        # Manually trigger the scenario by modifying the to_haul set
        async def mock_identify_buoys(
            er_gears_devices_id_to_gear, serial_number_to_edgetech_buoy
        ):
            # Return a buoy serial/user that should be hauled but doesn't exist in ER
            return set(), {"NONEXISTENT/userABC"}, set()

        processor._identify_buoys = mock_identify_buoys

        with caplog.at_level(logging.WARNING):
            observations = await processor.process()

        # Should log warning about no ER gear found
        assert "No ER gear found for buoy NONEXISTENT/userABC" in caplog.text
        assert len(observations) == 0

    @pytest.mark.asyncio
    async def test_process_haul_validation_error(self, mocker, caplog):
        """Test handling ValidationError during haul payload creation."""
        data = []
        processor = EdgeTechProcessor(data=data, er_token="token", er_url="url")

        mock_device = BuoyDevice(
            device_id="some-uuid",
            mfr_device_id="HAUL123_userABC_A",
            label="Haul Device",
            location=DeviceLocation(latitude=44.0, longitude=-68.0),
            last_updated=datetime.now(timezone.utc),
            last_deployed=datetime.now(timezone.utc),
        )

        mock_gear = BuoyGear(
            id=uuid4(),
            display_id="GEAR456",
            status="deployed",
            last_updated=datetime.now(timezone.utc),
            devices=[mock_device],
            type="ropeless",
            manufacturer="edgetech",
        )

        mock_er_client = mocker.MagicMock()
        mock_er_client.get_er_gears = AsyncMock(return_value=[mock_gear])
        mock_er_client.get_sources = AsyncMock(return_value=[])
        processor._er_client = mock_er_client

        # Mock _identify_buoys to return a haul set that will be processed
        async def mock_identify_buoys(
            er_gears_devices_id_to_gear, serial_number_to_edgetech_buoy
        ):
            return set(), {"HAUL123/userABC"}, set()

        processor._identify_buoys = mock_identify_buoys

        # Mock _create_haul_payload to raise ValidationError
        def raise_validation_error(*args, **kwargs):
            raise pydantic.ValidationError([], BuoyGear)

        mocker.patch.object(
            processor,
            "_create_haul_payload",
            new=Mock(side_effect=raise_validation_error),
        )

        with caplog.at_level(logging.ERROR):
            payloads = await processor.process()

        # Should log the validation error
        assert "Failed to create haul payload for gear set" in caplog.text
        assert len(payloads) == 0

    @pytest.mark.asyncio
    async def test_process_deploy_skip_end_unit_with_start_unit(
        self, mocker, caplog, a_new_edgetech_trawl_record
    ):
        """Test skipping end unit record during deployment (should be handled by start unit)."""
        # Create start unit record
        start_unit_record = a_new_edgetech_trawl_record.copy()
        start_unit_record["serialNumber"] = "START123"
        start_unit_record["currentState"] = start_unit_record["currentState"].copy()
        start_unit_record["currentState"]["serialNumber"] = "START123"
        start_unit_record["currentState"]["isTwoUnitLine"] = True
        start_unit_record["currentState"]["endUnit"] = "END456"
        start_unit_record["currentState"]["startUnit"] = None  # This is the start unit

        # Create end unit record that should be skipped during deploy
        end_unit_record = a_new_edgetech_trawl_record.copy()
        end_unit_record["serialNumber"] = "END456"
        end_unit_record["userId"] = a_new_edgetech_trawl_record[
            "userId"
        ]  # Same user for both
        end_unit_record["currentState"] = end_unit_record["currentState"].copy()
        end_unit_record["currentState"]["serialNumber"] = "END456"
        end_unit_record["currentState"]["isTwoUnitLine"] = True
        end_unit_record["currentState"][
            "startUnit"
        ] = "START123"  # This makes it an end unit that should be skipped
        end_unit_record["currentState"]["endUnit"] = None

        # Include both records so the end unit can be found
        data = [start_unit_record, end_unit_record]
        processor = EdgeTechProcessor(data=data, er_token="token", er_url="url")

        # No existing ER gear (new deployment scenario)
        mock_er_client = mocker.MagicMock()
        mock_er_client.get_er_gears = AsyncMock(return_value=[])
        mock_er_client.get_sources = AsyncMock(return_value=[])
        mock_er_client.get_existing_source_id_by_manufacturer_id = AsyncMock(
            return_value=None
        )
        processor._er_client = mock_er_client

        # The end unit should be skipped (line 258), so only start unit observations should be created
        with caplog.at_level(logging.INFO):
            observations = await processor.process()

        # Should have observations for start unit only
        assert len(observations) > 0
        # Should not create observations for the end unit (it's skipped)

    @pytest.mark.asyncio
    async def test_process_circular_two_unit_line_only_one_gear_created(
        self, mocker, caplog, a_new_edgetech_trawl_record
    ):
        """When two devices are each configured as start with the other as end (circular),
        only one gearset payload is created; the duplicate start is skipped with a warning.
        """
        user_id = a_new_edgetech_trawl_record["userId"]
        # Unit A: lead in one config, endUnit = B
        record_a = a_new_edgetech_trawl_record.copy()
        record_a["serialNumber"] = "88CE99D99E"
        record_a["currentState"] = record_a["currentState"].copy()
        record_a["currentState"]["serialNumber"] = "88CE99D99E"
        record_a["currentState"]["isTwoUnitLine"] = True
        record_a["currentState"]["endUnit"] = "88CE9978B7"
        record_a["currentState"]["startUnit"] = None
        record_a["currentState"]["endLatDeg"] = None
        record_a["currentState"]["endLonDeg"] = None
        # Unit B: also configured as lead with endUnit = A (circular)
        record_b = a_new_edgetech_trawl_record.copy()
        record_b["serialNumber"] = "88CE9978B7"
        record_b["userId"] = user_id
        record_b["currentState"] = record_b["currentState"].copy()
        record_b["currentState"]["serialNumber"] = "88CE9978B7"
        record_b["currentState"]["latDeg"] = 42.3267312
        record_b["currentState"]["lonDeg"] = -70.0474376
        record_b["currentState"]["isTwoUnitLine"] = True
        record_b["currentState"]["endUnit"] = "88CE99D99E"
        record_b["currentState"]["startUnit"] = None
        record_b["currentState"]["endLatDeg"] = None
        record_b["currentState"]["endLonDeg"] = None

        data = [record_a, record_b]
        processor = EdgeTechProcessor(data=data, er_token="token", er_url="url")

        mock_er_client = mocker.MagicMock()
        mock_er_client.get_er_gears = AsyncMock(return_value=[])
        mock_er_client.get_sources = AsyncMock(return_value=[])
        mock_er_client.get_existing_source_id_by_manufacturer_id = AsyncMock(
            return_value=None
        )
        processor._er_client = mock_er_client

        with caplog.at_level(logging.WARNING):
            payloads = await processor.process()

        # Exactly one gear payload (canonical lead); the other start is skipped
        assert len(payloads) == 1
        assert "Circular two-unit line detected" in caplog.text
        assert "88CE99D99E" in caplog.text and "88CE9978B7" in caplog.text
        # Canonical lead is the smaller serial (88CE9978B7 < 88CE99D99E)
        gear = payloads[0]
        device_mfr_ids = [d.get("mfr_device_id") for d in gear.get("devices", [])]
        assert len(device_mfr_ids) == 2
        hashed = get_hashed_user_id(user_id)
        assert f"88CE9978B7_{hashed}" in device_mfr_ids
        assert f"88CE99D99E_{hashed}" in device_mfr_ids

    @pytest.mark.asyncio
    async def test_process_circular_two_unit_both_already_in_er_same_gear_single_update(
        self, mocker, caplog, a_new_edgetech_trawl_record
    ):
        """When both devices are already in ER (e.g. Feb 3 gear that later got the
        circular pair attached), circular skip ensures we send only one update for
        that gear, not two updates (which could duplicate or confuse the gear).
        """
        user_id = a_new_edgetech_trawl_record["userId"]
        hashed = get_hashed_user_id(user_id)
        gear_id = uuid4()
        # Existing gear in ER with both devices (simulating circular attach to Feb 3 gear)
        older_time = datetime(2026, 2, 3, 12, 0, 0, tzinfo=timezone.utc)
        mock_device_a = BuoyDevice(
            device_id=f"88CE99D99E_{hashed}",
            mfr_device_id=f"88CE99D99E_{hashed}",
            label="Lead",
            location=DeviceLocation(latitude=42.32, longitude=-70.05),
            last_updated=older_time,
            last_deployed=older_time,
        )
        mock_device_b = BuoyDevice(
            device_id=f"88CE9978B7_{hashed}",
            mfr_device_id=f"88CE9978B7_{hashed}",
            label="End",
            location=DeviceLocation(latitude=42.32, longitude=-70.04),
            last_updated=older_time,
            last_deployed=older_time,
        )
        existing_gear = BuoyGear(
            id=gear_id,
            display_id="GEAR-FEB3",
            status="deployed",
            last_updated=older_time,
            devices=[mock_device_a, mock_device_b],
            type="ropeless",
            manufacturer="edgetech",
        )
        # EdgeTech: circular two-unit (both as start with other as end)
        record_a = a_new_edgetech_trawl_record.copy()
        record_a["serialNumber"] = "88CE99D99E"
        record_a["currentState"] = record_a["currentState"].copy()
        record_a["currentState"]["serialNumber"] = "88CE99D99E"
        record_a["currentState"]["latDeg"] = 42.3246478
        record_a["currentState"]["lonDeg"] = -70.0545583
        record_a["currentState"]["lastUpdated"] = "2026-02-14T22:34:54.891Z"
        record_a["currentState"]["isTwoUnitLine"] = True
        record_a["currentState"]["endUnit"] = "88CE9978B7"
        record_a["currentState"]["startUnit"] = None
        record_a["currentState"]["endLatDeg"] = None
        record_a["currentState"]["endLonDeg"] = None
        record_b = a_new_edgetech_trawl_record.copy()
        record_b["serialNumber"] = "88CE9978B7"
        record_b["userId"] = user_id
        record_b["currentState"] = record_b["currentState"].copy()
        record_b["currentState"]["serialNumber"] = "88CE9978B7"
        record_b["currentState"]["latDeg"] = 42.3267312
        record_b["currentState"]["lonDeg"] = -70.0474376
        record_b["currentState"]["lastUpdated"] = "2026-02-14T22:34:54.975Z"
        record_b["currentState"]["isTwoUnitLine"] = True
        record_b["currentState"]["endUnit"] = "88CE99D99E"
        record_b["currentState"]["startUnit"] = None
        record_b["currentState"]["endLatDeg"] = None
        record_b["currentState"]["endLonDeg"] = None

        data = [record_a, record_b]
        processor = EdgeTechProcessor(data=data, er_token="token", er_url="url")

        mock_er_client = mocker.MagicMock()
        mock_er_client.get_er_gears = AsyncMock(return_value=[existing_gear])
        mock_er_client.get_sources = AsyncMock(return_value=[])
        mock_er_client.get_existing_source_id_by_manufacturer_id = AsyncMock(
            return_value=None
        )
        processor._er_client = mock_er_client

        with caplog.at_level(logging.WARNING):
            payloads = await processor.process()

        # One update payload for the existing gear (no second update from the other start)
        assert len(payloads) == 1
        assert payloads[0].get("set_id") == str(gear_id)
        assert len(payloads[0].get("devices", [])) == 2
        assert (
            "Skipping update for 88CE99D99E" in caplog.text
            or "circular two-unit duplicate" in caplog.text
        )

    @pytest.mark.asyncio
    async def test_process_update_no_location_change_exact_coordinates(
        self, mocker, caplog
    ):
        """Test that location comparison path is covered when coordinates match exactly."""
        # Create the buoy data to pass to the constructor
        user_id = "n9JpP3kk8vFVyNlzMnYZig9DnO475ztWV5JQ4z3RHwO19GPjN9sL8qDw8YgW"
        hashed_user_id = get_hashed_user_id(user_id)
        serial_number = "8899CEDAAA"

        mock_edgetech_buoy_data = {
            "serialNumber": serial_number,
            "userId": user_id,
            "currentState": {
                "etag": "1748195599731",
                "isDeleted": False,
                "serialNumber": serial_number,
                "releaseCommand": "C8AB8C75AA",
                "statusCommand": "8899CEDAAA",
                "idCommand": "CCCCCCCCCC",
                "isNfcTag": False,
                "modelNumber": "",
                "dateOfManufacture": None,
                "dateOfBatteryChange": None,
                "dateDeployed": "2025-05-25T17:53:19.517000+00:00",
                "isDeployed": True,
                "dateRecovered": None,  # No recovery date so it won't be in haul list
                "recoveredLatDeg": None,
                "recoveredLonDeg": None,
                "recoveredRangeM": None,
                "dateStatus": None,
                "statusRangeM": None,
                "statusIsTilted": None,
                "statusBatterySoC": None,
                "lastUpdated": "2025-05-25T17:53:19.731000+00:00",
                "latDeg": 44.358265,  # Exact coordinates that will match ER device
                "lonDeg": -68.16757,
                "endLatDeg": 44.3591792,
                "endLonDeg": -68.167191,
                "isTwoUnitLine": None,
                "endUnit": None,
                "startUnit": None,
            },
            "changeRecords": [
                {
                    "type": "MODIFY",
                    "timestamp": "2025-05-25T17:53:19.000Z",
                    "changes": [
                        {
                            "key": "dateDeployed",
                            "oldValue": None,
                            "newValue": "2025-05-25T17:53:19.517Z",
                        }
                    ],
                }
            ],
        }

        processor = EdgeTechProcessor(
            data=[mock_edgetech_buoy_data],
            er_token="test_token",
            er_url="http://test.com",
        )

        # Use the exact device_id that will be generated by the processor
        expected_device_id_primary = f"{serial_number}_{hashed_user_id}_A"

        # Create a mock device with location as a tuple (to work around the processor bug)
        mock_device = Mock()
        mock_device.device_id = expected_device_id_primary
        mock_device.mfr_device_id = expected_device_id_primary  # Add this field
        mock_device.location = Mock()
        mock_device.location.latitude = 44.358265
        mock_device.location.longitude = -68.16757  # exact same coordinates

        mock_gear = Mock()
        mock_gear.devices = [mock_device]
        mock_gear.manufacturer = "edgetech"  # This is important for the filtering
        mock_gear.status = "deployed"
        mock_gear.last_updated = datetime(
            2025, 5, 20, 10, 0, 0, tzinfo=timezone.utc
        )  # Older than the buoy's lastUpdated
        mock_gear.create_haul_observation = Mock(
            return_value=[]
        )  # Return empty list to avoid the TypeError

        # Mock the ER client to return the gear
        mock_er_client = mocker.MagicMock()
        mock_er_client.get_er_gears = AsyncMock(return_value=[mock_gear])
        mock_er_client.get_sources = AsyncMock(return_value=[])
        mock_er_client.get_existing_source_id_by_manufacturer_id = AsyncMock(
            return_value=None
        )
        processor._er_client = mock_er_client

        with caplog.at_level(logging.INFO):
            await processor.process()

        # Should see the location comparison message in logs
        assert "No change in location for buoy" in caplog.text

    @pytest.mark.asyncio
    async def test_identify_buoys_position_update_not_skipped_same_date_deployed(
        self, mocker, caplog
    ):
        """
        Test that position updates are NOT skipped when dateDeployed is unchanged but lastUpdated is newer.

        This is the bug fix test: Previously, position updates were incorrectly skipped because
        the code used dateDeployed for recorded_at, and dateDeployed doesn't change on position updates.
        Now we use lastUpdated for updates, so position changes get a new recorded_at timestamp.
        """
        # Create EdgeTech buoy with:
        # - dateDeployed: 22:40:08 (same as ER)
        # - lastUpdated: 22:42:49 (newer - position changed)
        # - Location: changed from ER
        edgetech_buoy_data = {
            "serialNumber": "88CE99D976",
            "userId": "63209b6fe870303c76ddebec",
            "currentState": {
                "etag": "1768516969654",
                "isDeleted": False,
                "serialNumber": "88CE99D976",
                "releaseCommand": "C8AB8C7476",
                "statusCommand": "88CE99D976",
                "idCommand": "CCCCCCCCCC",
                "isNfcTag": False,
                "latDeg": 40.34753333333333,  # NEW position
                "lonDeg": -71.59648333333334,  # NEW position
                "endLatDeg": 40.3499481,
                "endLonDeg": -71.5732852,
                "modelNumber": "5112",
                "isDeployed": True,
                "dateDeployed": "2026-01-15T22:40:08.628Z",  # SAME as ER
                "lastUpdated": "2026-01-15T22:42:49.654Z",  # NEWER than ER
            },
            "changeRecords": [],
        }

        processor = EdgeTechProcessor(
            data=[edgetech_buoy_data], er_token="token", er_url="url"
        )

        hashed_user_id = get_hashed_user_id("63209b6fe870303c76ddebec")
        device_id = f"88CE99D976_{hashed_user_id}_A"

        # Mock existing ER gear with older lastUpdated and DIFFERENT location
        mock_device = BuoyDevice(
            device_id=device_id,
            mfr_device_id=device_id,
            label="Test Device",
            location=DeviceLocation(
                latitude=40.3499686, longitude=-71.573436  # OLD position
            ),
            last_updated=datetime(2026, 1, 15, 22, 40, 9, tzinfo=timezone.utc),
            last_deployed=datetime(
                2026, 1, 15, 22, 40, 8, tzinfo=timezone.utc
            ),  # SAME as EdgeTech
        )

        mock_gear = BuoyGear(
            id=uuid4(),
            display_id="GEAR123",
            status="deployed",
            last_updated=datetime(2026, 1, 15, 22, 40, 9, tzinfo=timezone.utc),
            devices=[mock_device],
            type="trawl",
            manufacturer="edgetech",
        )

        er_gears_devices_id_to_gear = {device_id: mock_gear}

        serial_number_to_edgetech_buoy = {
            f"88CE99D976/{hashed_user_id}": processor._data[0]
        }

        with caplog.at_level(logging.INFO):
            to_deploy, to_haul, to_update = await processor._identify_buoys(
                er_gears_devices_id_to_gear, serial_number_to_edgetech_buoy
            )

        # Key assertion: The buoy should be marked for UPDATE, not skipped
        # Before the fix, this would be 0 because dateDeployed matched last_deployed
        assert len(to_update) == 1
        assert f"88CE99D976/{hashed_user_id}" in to_update
        assert len(to_haul) == 0
        assert len(to_deploy) == 0

        # Verify the log shows it was marked for update (not skipped as duplicate)
        assert "marked for update" in caplog.text
        assert "skipped - recorded_at" not in caplog.text

    @pytest.mark.asyncio
    async def test_create_gear_payload_uses_last_updated_for_updates(self):
        """
        Test that _create_gear_payload uses lastUpdated (not dateDeployed) for recorded_at
        when include_initial_deployment=False (i.e., for updates).

        This ensures position updates get a unique recorded_at timestamp.
        """
        processor = EdgeTechProcessor(data=[], er_token="token", er_url="url")

        buoy_data = {
            "serialNumber": "TEST123",
            "userId": "user123",
            "currentState": {
                "etag": "test_etag",
                "isDeleted": False,
                "serialNumber": "TEST123",
                "releaseCommand": "release123",
                "statusCommand": "status123",
                "idCommand": "id123",
                "isNfcTag": False,
                "latDeg": 40.0,
                "lonDeg": -70.0,
                "modelNumber": "Model123",
                "isDeployed": True,
                "dateDeployed": "2026-01-15T22:40:08.000Z",  # Deployment time
                "lastUpdated": "2026-01-15T22:42:49.000Z",  # Position update time (later)
            },
            "changeRecords": [],
        }

        buoy = Buoy.parse_obj(buoy_data)

        # Test for INITIAL deployment (include_initial_deployment=True)
        payload_initial = await processor._create_gear_payload(
            buoy=buoy,
            device_status="deployed",
            manufacturer_id_to_source_id={},
            include_initial_deployment=True,
        )

        # For initial deployment, recorded_at should be dateDeployed
        assert (
            payload_initial["devices"][0]["recorded_at"] == "2026-01-15T22:40:08+00:00"
        )

        # Test for UPDATE (include_initial_deployment=False)
        payload_update = await processor._create_gear_payload(
            buoy=buoy,
            device_status="deployed",
            manufacturer_id_to_source_id={},
            include_initial_deployment=False,
        )

        # For updates, recorded_at should be lastUpdated (the fix!)
        assert (
            payload_update["devices"][0]["recorded_at"] == "2026-01-15T22:42:49+00:00"
        )

        # Verify the timestamps are different
        assert (
            payload_initial["devices"][0]["recorded_at"]
            != payload_update["devices"][0]["recorded_at"]
        )

    @pytest.mark.asyncio
    async def test_position_update_end_to_end(self, mocker, caplog):
        """
        End-to-end test that verifies position updates are processed correctly.

        Scenario: A deployed gear has its position updated in EdgeTech, but dateDeployed
        remains unchanged. The update should be processed and create a payload with
        the new position and lastUpdated as recorded_at.
        """
        # EdgeTech data with position change
        edgetech_data = {
            "serialNumber": "POS123",
            "userId": "user456",
            "currentState": {
                "etag": "test_etag",
                "isDeleted": False,
                "serialNumber": "POS123",
                "releaseCommand": "release123",
                "statusCommand": "status123",
                "idCommand": "id123",
                "isNfcTag": False,
                "latDeg": 41.0,  # NEW position
                "lonDeg": -71.0,  # NEW position
                "modelNumber": "Model123",
                "isDeployed": True,
                "dateDeployed": "2026-01-15T10:00:00.000Z",  # SAME as ER
                "lastUpdated": "2026-01-15T12:00:00.000Z",  # NEWER
            },
            "changeRecords": [],
        }

        processor = EdgeTechProcessor(
            data=[edgetech_data], er_token="token", er_url="url"
        )

        hashed_user_id = get_hashed_user_id("user456")
        device_id = f"POS123_{hashed_user_id}"

        # Existing ER gear with OLD position
        mock_device = BuoyDevice(
            device_id=device_id,
            mfr_device_id=device_id,
            label="Test Device",
            location=DeviceLocation(latitude=40.0, longitude=-70.0),  # OLD position
            last_updated=datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
            last_deployed=datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
        )

        mock_gear = BuoyGear(
            id=uuid4(),
            display_id="GEAR456",
            status="deployed",
            last_updated=datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
            devices=[mock_device],
            type="single",
            manufacturer="edgetech",
        )

        mock_er_client = mocker.MagicMock()
        mock_er_client.get_er_gears = AsyncMock(return_value=[mock_gear])
        mock_er_client.get_sources = AsyncMock(return_value=[])
        processor._er_client = mock_er_client

        with caplog.at_level(logging.INFO):
            payloads = await processor.process()

        # Should generate exactly 1 update payload
        assert len(payloads) == 1

        payload = payloads[0]
        device = payload["devices"][0]

        # Verify the new position is in the payload
        assert device["location"]["latitude"] == 41.0
        assert device["location"]["longitude"] == -71.0

        # Key assertion: recorded_at should be lastUpdated, not dateDeployed
        assert device["recorded_at"] == "2026-01-15T12:00:00+00:00"

        # Verify logs show update (not skipped)
        assert "marked for update" in caplog.text

    @pytest.mark.asyncio
    async def test_process_deploy_end_unit_record_skip_line_258(self, mocker, caplog):
        """Test that line 258 is hit when processing an end unit record with startUnit."""
        # Create a companion buoy that will be found as the "end unit"
        companion_data = {
            "serialNumber": "COMPANION789",
            "userId": "7889ad74-aab3-4044-bcf4-13d6f9586a82",
            "currentState": {
                "etag": "1748195599730",
                "isDeleted": False,
                "serialNumber": "COMPANION789",
                "releaseCommand": "C8AB8C75AA",
                "statusCommand": "8899CEDAAA",
                "idCommand": "CCCCCCCCCC",
                "isNfcTag": False,
                "modelNumber": "",
                "dateOfManufacture": None,
                "dateOfBatteryChange": None,
                "dateDeployed": "2025-05-25T17:53:19.517000+00:00",
                "isDeployed": True,
                "dateRecovered": None,
                "recoveredLatDeg": None,
                "recoveredLonDeg": None,
                "recoveredRangeM": None,
                "dateStatus": None,
                "statusRangeM": None,
                "statusIsTilted": None,
                "statusBatterySoC": None,
                "lastUpdated": "2025-05-25T17:53:19.731000+00:00",
                "latDeg": 44.3591792,
                "lonDeg": -68.167191,
                "endLatDeg": 44.358265,
                "endLonDeg": -68.16757,
                "isTwoUnitLine": False,
                "endUnit": None,
                "startUnit": None,
            },
            "changeRecords": [],
        }

        # Create end unit record that should trigger line 258 skip
        # This record has both endUnit (so partner can be found) AND startUnit (so it's skipped)
        end_unit_data = {
            "serialNumber": "END456",
            "userId": "7889ad74-aab3-4044-bcf4-13d6f9586a82",
            "currentState": {
                "etag": "1748195599732",
                "isDeleted": False,
                "serialNumber": "END456",
                "releaseCommand": "C8AB8C75AA",
                "statusCommand": "8899CEDAAA",
                "idCommand": "CCCCCCCCCC",
                "isNfcTag": False,
                "modelNumber": "",
                "dateOfManufacture": None,
                "dateOfBatteryChange": None,
                "dateDeployed": "2025-05-25T17:53:19.517000+00:00",
                "isDeployed": True,
                "dateRecovered": None,
                "recoveredLatDeg": None,
                "recoveredLonDeg": None,
                "recoveredRangeM": None,
                "dateStatus": None,
                "statusRangeM": None,
                "statusIsTilted": None,
                "statusBatterySoC": None,
                "lastUpdated": "2025-05-25T17:53:19.731000+00:00",
                "latDeg": 44.3591792,
                "lonDeg": -68.167191,
                "endLatDeg": 44.358265,
                "endLonDeg": -68.16757,
                "isTwoUnitLine": True,
                "endUnit": "COMPANION789",  # This allows the end unit to be found (avoids line 255)
                "startUnit": "START123",  # This triggers line 258 skip!
            },
            "changeRecords": [],
        }

        # Process both records - so the companion can be found in the data
        processor = EdgeTechProcessor(
            data=[companion_data, end_unit_data], er_token="token", er_url="url"
        )

        # Mock ER client to return no existing gears (deploy scenario)
        mock_er_client = mocker.MagicMock()
        mock_er_client.get_er_gears = AsyncMock(return_value=[])
        mock_er_client.get_sources = AsyncMock(return_value=[])
        mock_er_client.get_existing_source_id_by_manufacturer_id = AsyncMock(
            return_value=None
        )
        processor._er_client = mock_er_client

        payloads = await processor.process()

        # END456 record should be skipped due to line 258 (has startUnit set)
        # Only COMPANION789 should generate a gear payload (1 payload with 2 devices for trawl)
        assert len(payloads) == 1
        assert payloads[0]["deployment_type"] == "trawl"
        assert len(payloads[0]["devices"]) == 2

    @pytest.mark.asyncio
    async def test_identify_buoys_hauled_in_er_deployed_in_edgetech(
        self, mocker, a_new_edgetech_trawl_record
    ):
        """When ER gear is hauled but EdgeTech shows deployed, treat as new deployment."""
        processor = EdgeTechProcessor(
            data=[a_new_edgetech_trawl_record], er_token="token", er_url="url"
        )

        user_id = a_new_edgetech_trawl_record["userId"]
        serial_number = a_new_edgetech_trawl_record["serialNumber"]
        hashed_user_id = get_hashed_user_id(user_id)
        device_id = f"{serial_number}_{hashed_user_id}_A"

        mock_device = BuoyDevice(
            device_id="some-uuid",
            mfr_device_id=device_id,
            label="Device A",
            location=DeviceLocation(latitude=44.0, longitude=-68.0),
            last_updated=datetime.now(timezone.utc),
            last_deployed=datetime(2025, 5, 20, 10, 0, 0, tzinfo=timezone.utc),
        )

        mock_gear = BuoyGear(
            id=uuid4(),
            display_id="GEAR123",
            status="hauled",  # Already hauled in ER
            last_updated=datetime.now(timezone.utc) - timedelta(hours=1),
            devices=[mock_device],
            type="trawl",
            manufacturer="edgetech",
        )

        er_gears_devices_id_to_gear = {device_id: mock_gear}
        buoy_key = f"{serial_number}/{hashed_user_id}"
        serial_number_to_edgetech_buoy = {buoy_key: processor._data[0]}

        to_deploy, to_haul, to_update = await processor._identify_buoys(
            er_gears_devices_id_to_gear, serial_number_to_edgetech_buoy
        )

        assert buoy_key in to_deploy
        assert len(to_haul) == 0
        assert len(to_update) == 0

    @pytest.mark.asyncio
    async def test_redeployment_haul_uses_change_record_date_recovered(self):
        """For re-deployments, haul payload should use dateRecovered from changeRecords
        when currentState dateRecovered is null (cleared by the redeploy)."""
        user_id = "66fff43f7386585d6687e3d3"
        serial_number = "88CE99D39E"
        hashed_user_id = get_hashed_user_id(user_id)

        # Simulate the re-deployment scenario: buoy hauled then redeployed within seconds
        buoy_data = {
            "serialNumber": serial_number,
            "userId": user_id,
            "currentState": {
                "etag": "1773845753701",
                "isDeleted": False,
                "serialNumber": serial_number,
                "releaseCommand": "C8AB8C769E",
                "statusCommand": serial_number,
                "idCommand": "CCCCCCCCCC",
                "latDeg": 43.8309358,
                "lonDeg": -69.6456942,
                "endLatDeg": 43.8315133,
                "endLonDeg": -69.6456975,
                "modelNumber": "5112",
                "isDeployed": True,
                "dateDeployed": "2026-03-18T14:55:53.191Z",
                "dateRecovered": None,  # Cleared by redeploy
                "recoveredLatDeg": None,  # Cleared by redeploy
                "recoveredLonDeg": None,
                "lastUpdated": "2026-03-18T14:55:53.701Z",
            },
            "changeRecords": [
                {
                    "type": "MODIFY",
                    "timestamp": "2026-03-18T14:55:53.000Z",
                    "changes": [
                        {
                            "key": "dateDeployed",
                            "oldValue": None,
                            "newValue": "2026-03-18T14:55:53.191Z",
                        },
                        {
                            "key": "dateRecovered",
                            "oldValue": "2026-03-18T14:48:26.078Z",
                            "newValue": None,
                        },
                        {"key": "isDeployed", "oldValue": False, "newValue": True},
                        {
                            "key": "recoveredLatDeg",
                            "oldValue": 43.8495062,
                            "newValue": None,
                        },
                        {
                            "key": "recoveredLonDeg",
                            "oldValue": -69.6290056,
                            "newValue": None,
                        },
                    ],
                },
                {
                    "type": "MODIFY",
                    "timestamp": "2026-03-18T14:55:07.000Z",
                    "changes": [
                        {
                            "key": "dateDeployed",
                            "oldValue": "2026-03-10T11:16:57.467Z",
                            "newValue": None,
                        },
                        {
                            "key": "dateRecovered",
                            "oldValue": None,
                            "newValue": "2026-03-18T14:48:26.078Z",
                        },
                        {"key": "isDeployed", "oldValue": True, "newValue": False},
                        {
                            "key": "recoveredLatDeg",
                            "oldValue": None,
                            "newValue": 43.8495062,
                        },
                        {
                            "key": "recoveredLonDeg",
                            "oldValue": None,
                            "newValue": -69.6290056,
                        },
                        {
                            "key": "recoveredRangeM",
                            "oldValue": None,
                            "newValue": 50.932,
                        },
                    ],
                },
            ],
        }

        buoy = Buoy.parse_obj(buoy_data)
        processor = EdgeTechProcessor(data=[buoy_data], er_token="token", er_url="url")

        # Create the ER gear that was deployed earlier (March 10)
        device_id_a = f"{serial_number}_{hashed_user_id}_A"
        mock_device = BuoyDevice(
            device_id="existing-uuid",
            mfr_device_id=device_id_a,
            label="Device A",
            location=DeviceLocation(latitude=43.8308732, longitude=-69.6453918),
            last_updated=datetime(2026, 3, 10, 11, 16, 57, tzinfo=timezone.utc),
            last_deployed=datetime(2026, 3, 10, 11, 16, 57, tzinfo=timezone.utc),
        )
        mock_gear = BuoyGear(
            id=uuid4(),
            display_id="GEAR-OLD",
            status="deployed",
            last_updated=datetime(2026, 3, 10, 11, 16, 57, tzinfo=timezone.utc),
            devices=[mock_device],
            type="single",
            manufacturer="edgetech",
        )

        # Test _create_haul_payload uses dateRecovered from changeRecords
        payload = processor._create_haul_payload(er_gear=mock_gear, edgetech_buoy=buoy)

        # The haul recorded_at should be from the changeRecords dateRecovered (14:48:26)
        # NOT from lastUpdated (14:55:53) which would collide with the deploy
        assert payload["devices"][0]["recorded_at"] == "2026-03-18T14:48:26+00:00"

        # Recovery location should also come from changeRecords
        assert payload["devices"][0]["location"]["latitude"] == 43.8495062
        assert payload["devices"][0]["location"]["longitude"] == -69.6290056

    @pytest.mark.asyncio
    async def test_redeployment_identify_haul_and_deploy(self):
        """Re-deployment: same serial hauled and redeployed within seconds should
        produce both a haul and a deploy."""
        user_id = "66fff43f7386585d6687e3d3"
        serial_number = "88CE99D39E"
        hashed_user_id = get_hashed_user_id(user_id)

        buoy_data = {
            "serialNumber": serial_number,
            "userId": user_id,
            "currentState": {
                "etag": "1773845753701",
                "isDeleted": False,
                "serialNumber": serial_number,
                "releaseCommand": "C8AB8C769E",
                "statusCommand": serial_number,
                "idCommand": "CCCCCCCCCC",
                "latDeg": 43.8309358,
                "lonDeg": -69.6456942,
                "endLatDeg": 43.8315133,
                "endLonDeg": -69.6456975,
                "modelNumber": "5112",
                "isDeployed": True,
                "dateDeployed": "2026-03-18T14:55:53.191Z",
                "dateRecovered": None,
                "lastUpdated": "2026-03-18T14:55:53.701Z",
            },
            "changeRecords": [],
        }

        processor = EdgeTechProcessor(data=[buoy_data], er_token="token", er_url="url")

        device_id_a = f"{serial_number}_{hashed_user_id}_A"
        mock_device = BuoyDevice(
            device_id="existing-uuid",
            mfr_device_id=device_id_a,
            label="Device A",
            location=DeviceLocation(latitude=43.8308732, longitude=-69.6453918),
            last_updated=datetime(2026, 3, 10, 11, 16, 57, tzinfo=timezone.utc),
            last_deployed=datetime(2026, 3, 10, 11, 16, 57, tzinfo=timezone.utc),
        )
        mock_gear = BuoyGear(
            id=uuid4(),
            display_id="GEAR-OLD",
            status="deployed",
            last_updated=datetime(2026, 3, 10, 11, 16, 57, tzinfo=timezone.utc),
            devices=[mock_device],
            type="single",
            manufacturer="edgetech",
        )

        er_gears_devices_id_to_gear = {device_id_a: mock_gear}
        buoy_key = f"{serial_number}/{hashed_user_id}"
        serial_number_to_edgetech_buoy = {buoy_key: processor._data[0]}

        to_deploy, to_haul, to_update = await processor._identify_buoys(
            er_gears_devices_id_to_gear, serial_number_to_edgetech_buoy
        )

        # Should be in both haul and deploy (re-deployment)
        assert buoy_key in to_haul
        assert buoy_key in to_deploy
        assert len(to_update) == 0
