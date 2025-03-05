from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.actions.edgetech.processor import EdgetTechProcessor
from app.actions.edgetech.types import Buoy


# A helper to create minimal dummy buoy records that can be parsed by Buoy.parse_obj.
def get_dummy_buoy_record(
    serial, last_updated, lat, lon, is_deleted=False, is_deployed=True
):
    return {
        "serialNumber": serial,
        "currentState": {
            "etag": "dummy_etag",
            "isDeleted": is_deleted,
            "serialNumber": serial,
            "releaseCommand": "dummy_release",
            "statusCommand": "dummy_status",
            "idCommand": "dummy_id",
            "lastUpdated": last_updated.isoformat().replace("+00:00", "Z"),
            "latDeg": lat,
            "lonDeg": lon,
        },
        "changeRecords": [],
    }


# Dummy classes to simulate ER subject objects.
class DummyGeometry:
    def __init__(self, coordinates):
        self.coordinates = coordinates


class DummyPosition:
    def __init__(self, coordinates):
        self.geometry = DummyGeometry(coordinates)


class DummyERSubject:
    def __init__(self, name, is_active, coordinates):
        self.name = name
        self.is_active = is_active
        self.last_position = DummyPosition(coordinates)


# Patch the Buoy.create_observation method on the class to return a predictable value.
@pytest.fixture(autouse=True)
def patch_create_observation():
    original_method = Buoy.create_observations
    Buoy.create_observations = lambda self, prefix: [
        {"name": f"{prefix}{self.serialNumber}"}
    ]
    yield
    # Restore the original method after the test runs.
    Buoy.create_observations = original_method


@pytest.mark.asyncio
async def test_process_inserts_when_no_er_subjects():
    """
    When no ER subjects exist, valid buoy records should be processed as inserts.
    Only the newest record per serial is used, and records with missing location are skipped.
    """
    now = datetime.now(timezone.utc)
    # Two records for serial "B1": one recent and one older.
    record1 = get_dummy_buoy_record("B1", now, 40.0, -70.0)
    record2 = get_dummy_buoy_record("B1", now - timedelta(days=1), 40.0, -70.0)
    # Record for "B2" with missing location (will be skipped).
    record3 = get_dummy_buoy_record("B2", now, None, None)
    data = [record1, record2, record3]

    processor = EdgetTechProcessor(data, er_token="dummy", er_url="dummy_url")
    # Patch the ER client's get_er_subjects to return an empty list.
    processor._er_client.get_er_subjects = AsyncMock(return_value=[])

    observations = await processor.process()
    # Only serial "B1" qualifies (with the most recent record) and should yield one observation.
    assert len(observations) == 1
    assert observations[0]["name"] == "edgetech_B1"


@pytest.mark.asyncio
async def test_process_noop_when_er_subject_equivalent():
    """
    When an ER subject exists and is equivalent to the buoy record,
    the processor should treat it as a no‑op (i.e. no observation is generated).
    """
    now = datetime.now(timezone.utc)
    record = get_dummy_buoy_record("B1", now, 40.0, -70.0)
    data = [record]

    processor = EdgetTechProcessor(data, er_token="dummy", er_url="dummy_url")
    # Create a dummy ER subject that exactly matches the buoy record.
    dummy_er_subject = DummyERSubject("edgetech_B1_A", True, [-70.0, 40.0])
    processor._er_client.get_er_subjects = AsyncMock(return_value=[dummy_er_subject])

    observations = await processor.process()
    # Since the ER subject is equivalent, no observation should be created.
    assert len(observations) == 0


@pytest.mark.asyncio
async def test_process_updates_when_er_subject_not_equivalent():
    """
    When an ER subject exists but is not equivalent (e.g. location mismatch),
    the processor should generate an update observation.
    """
    now = datetime.now(timezone.utc)
    record = get_dummy_buoy_record("B1", now, 40.0, -70.0)
    data = [record]

    processor = EdgetTechProcessor(data, er_token="dummy", er_url="dummy_url")
    # Create a dummy ER subject with different coordinates.
    dummy_er_subject = DummyERSubject("edgetech_B1_A", True, [-70.1, 40.1])
    processor._er_client.get_er_subjects = AsyncMock(return_value=[dummy_er_subject])

    observations = await processor.process()
    # An update should be generated because the ER subject is not equivalent.
    assert len(observations) == 1
    assert observations[0]["name"] == "edgetech_B1"


@pytest.mark.asyncio
async def test_filter_data_respects_time_window():
    """
    Test that the _filter_data method only returns buoy records
    whose lastUpdated timestamps fall within the default filter window (last 180 days).
    """
    now = datetime.now(timezone.utc)
    # One record within 180 days and one record older than 180 days.
    record_recent = get_dummy_buoy_record("B1", now - timedelta(days=10), 40.0, -70.0)
    record_old = get_dummy_buoy_record("B2", now - timedelta(days=200), 41.0, -71.0)
    data = [record_recent, record_old]

    processor = EdgetTechProcessor(data, er_token="dummy", er_url="dummy_url")
    filtered = processor._filter_data()
    # Only the recent record should be returned.
    serials = [buoy.serialNumber for buoy in filtered]
    assert "B1" in serials
    assert "B2" not in serials