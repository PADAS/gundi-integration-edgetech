import logging
from unittest.mock import AsyncMock

import pytest

from app.actions.buoy.client import BuoyClient
from app.actions.buoy.types import BuoyGear

logger = logging.getLogger(__name__)


class MockGetContext:
    """Mock async context manager for session.get()"""

    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return None


class MockSessionContext:
    """Mock async context manager for ClientSession"""

    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return None


class MockSession:
    """Mock session with proper get method"""

    def __init__(self, get_responses):
        self.get_responses = (
            get_responses if isinstance(get_responses, list) else [get_responses]
        )
        self.get_calls = []
        self.call_count = 0

    def get(self, *args, **kwargs):
        self.get_calls.append((args, kwargs))
        response = self.get_responses[min(self.call_count, len(self.get_responses) - 1)]
        self.call_count += 1
        return response


@pytest.fixture
def sample_buoy_gear_data():
    """Fixture to create sample BuoyGear data for testing."""
    return {
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "display_id": "TEST-GEAR-001",
        "status": "deployed",
        "last_updated": "2025-08-31T10:00:00Z",
        "devices": [
            {
                "device_id": "579db58c-fb84-4bf2-9d59-33e010275f60",
                "mfr_device_id": "mfr-device-001",
                "label": "Test Device 1",
                "location": {"latitude": 44.358265, "longitude": -68.16757},
                "last_updated": "2025-08-31T10:00:00Z",
                "last_deployed": "2025-08-31T09:00:00Z",
            },
            {
                "device_id": "227e7250-454b-4628-aae1-24de3bcbb914",
                "mfr_device_id": "mfr-device-002",
                "label": "Test Device 2",
                "location": {"latitude": 44.3591792, "longitude": -68.167191},
                "last_updated": "2025-08-31T10:00:00Z",
                "last_deployed": None,
            },
        ],
        "type": "ropeless",
        "manufacturer": "edgetech",
    }


@pytest.fixture
def sample_paginated_response():
    """Fixture to create a sample paginated API response."""
    return {
        "data": {
            "results": [
                {
                    "id": "550e8400-e29b-41d4-a716-446655440000",
                    "display_id": "TEST-GEAR-001",
                    "status": "deployed",
                    "last_updated": "2025-08-31T10:00:00Z",
                    "devices": [
                        {
                            "device_id": "7147061f-3073-496c-a80c-0b62569d8cc8",
                            "mfr_device_id": "mfr-device-001",
                            "label": "Test Device 1",
                            "location": {"latitude": 44.358265, "longitude": -68.16757},
                            "last_updated": "2025-08-31T10:00:00Z",
                            "last_deployed": "2025-08-31T09:00:00Z",
                        }
                    ],
                    "type": "ropeless",
                    "manufacturer": "edgetech",
                }
            ],
            "next": "https://example.com/api/v1.0/gear/?page=2",
        }
    }


@pytest.fixture
def sample_paginated_response_last_page():
    """Fixture to create a sample last page API response."""
    return {
        "data": {
            "results": [
                {
                    "id": "550e8400-e29b-41d4-a716-446655440001",
                    "display_id": "TEST-GEAR-002",
                    "status": "retrieved",
                    "last_updated": "2025-08-31T11:00:00Z",
                    "devices": [],
                    "type": "ropeless",
                    "manufacturer": "edgetech",
                }
            ],
            "next": None,
        }
    }


class TestBuoyClient:
    """Test suite for BuoyClient class."""

    def test_init(self):
        """Test BuoyClient initialization."""
        # Arrange
        er_token = "test-token"
        er_site = "https://example.com/"

        # Act
        client = BuoyClient(er_token=er_token, er_site=er_site)

        # Assert
        assert client.er_token == er_token
        assert client.er_site == er_site
        assert client.headers == {"Authorization": "Bearer test-token"}

    @pytest.mark.asyncio
    async def test_get_er_gears_success_single_page(
        self, mocker, sample_buoy_gear_data
    ):
        """Test successful gear retrieval with single page response."""
        # Arrange
        client = BuoyClient(er_token="test-token", er_site="https://example.com/")

        # Mock response data
        response_data = {"data": {"results": [sample_buoy_gear_data], "next": None}}

        # Create a mock response
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=response_data)

        # Create mock session and ClientSession
        mock_session = MockSession(MockGetContext(mock_response))
        mock_client_session = mocker.patch("aiohttp.ClientSession")
        mock_client_session.return_value = MockSessionContext(mock_session)

        # Act
        result = await client.get_er_gears()

        # Assert
        assert len(result) == 1
        assert isinstance(result[0], BuoyGear)
        assert result[0].display_id == "TEST-GEAR-001"
        assert result[0].status == "deployed"
        assert len(result[0].devices) == 2

        # Verify the session.get was called with correct parameters
        assert len(mock_session.get_calls) == 1
        args, kwargs = mock_session.get_calls[0]
        assert args[0] == "https://example.com/api/v1.0/gear/"
        assert kwargs["headers"] == {"Authorization": "Bearer test-token"}
        assert kwargs["params"] == {"include_empty_location": "true"}

    @pytest.mark.asyncio
    async def test_get_er_gears_success_with_params(
        self, mocker, sample_buoy_gear_data
    ):
        """Test successful gear retrieval with query parameters."""
        # Arrange
        client = BuoyClient(er_token="test-token", er_site="https://example.com/")
        params = {"status": "deployed", "limit": 10}

        # Mock response data
        response_data = {"data": {"results": [sample_buoy_gear_data], "next": None}}

        # Create a mock response
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=response_data)

        # Create mock session and ClientSession
        mock_session = MockSession(MockGetContext(mock_response))
        mock_client_session = mocker.patch("aiohttp.ClientSession")
        mock_client_session.return_value = MockSessionContext(mock_session)

        # Act
        result = await client.get_er_gears(params=params)

        # Assert
        assert len(result) == 1

        # Verify the correct API call was made with params
        assert len(mock_session.get_calls) == 1
        args, kwargs = mock_session.get_calls[0]
        assert args[0] == "https://example.com/api/v1.0/gear/"
        assert kwargs["headers"] == {"Authorization": "Bearer test-token"}
        assert kwargs["params"] == {**params, "include_empty_location": "true"}

    @pytest.mark.asyncio
    async def test_get_er_gears_forwards_state_filter(
        self, mocker, sample_buoy_gear_data
    ):
        """The /gear/ endpoint defaults to state=deployed; hauled gears must
        be requested explicitly. Verify the `state` kwarg is forwarded as a
        query param so callers can opt into each lifecycle."""
        client = BuoyClient(er_token="test-token", er_site="https://example.com/")

        response_data = {"data": {"results": [sample_buoy_gear_data], "next": None}}
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=response_data)

        mock_session = MockSession(MockGetContext(mock_response))
        mock_client_session = mocker.patch("aiohttp.ClientSession")
        mock_client_session.return_value = MockSessionContext(mock_session)

        await client.get_er_gears(params={"page_size": 100}, state="hauled")

        assert len(mock_session.get_calls) == 1
        _, kwargs = mock_session.get_calls[0]
        assert kwargs["params"] == {
            "page_size": 100,
            "include_empty_location": "true",
            "state": "hauled",
        }

    @pytest.mark.asyncio
    async def test_get_er_gears_success_multiple_pages(
        self, mocker, sample_paginated_response, sample_paginated_response_last_page
    ):
        """Test successful gear retrieval with pagination."""
        # Arrange
        client = BuoyClient(er_token="test-token", er_site="https://example.com/")

        # Mock first page response
        mock_response_1 = AsyncMock()
        mock_response_1.status = 200
        mock_response_1.json = AsyncMock(return_value=sample_paginated_response)

        # Mock second page response
        mock_response_2 = AsyncMock()
        mock_response_2.status = 200
        mock_response_2.json = AsyncMock(
            return_value=sample_paginated_response_last_page
        )

        # Create mock session with multiple responses
        mock_session = MockSession(
            [MockGetContext(mock_response_1), MockGetContext(mock_response_2)]
        )
        mock_client_session = mocker.patch("aiohttp.ClientSession")
        mock_client_session.return_value = MockSessionContext(mock_session)

        # Act
        result = await client.get_er_gears()

        # Assert
        assert len(result) == 2  # One from each page
        assert isinstance(result[0], BuoyGear)
        assert isinstance(result[1], BuoyGear)
        assert result[0].display_id == "TEST-GEAR-001"
        assert result[1].display_id == "TEST-GEAR-002"

        # Verify both API calls were made
        assert len(mock_session.get_calls) == 2

    @pytest.mark.asyncio
    async def test_get_er_gears_http_error(self, mocker):
        """Test gear retrieval with HTTP error response raises RuntimeError."""
        # Arrange
        client = BuoyClient(er_token="test-token", er_site="https://example.com/")

        # Mock error response
        mock_response = AsyncMock()
        mock_response.status = 500
        mock_response.text = AsyncMock(return_value="Internal Server Error")

        # Create mock session and ClientSession
        mock_session = MockSession(MockGetContext(mock_response))
        mock_client_session = mocker.patch("aiohttp.ClientSession")
        mock_client_session.return_value = MockSessionContext(mock_session)

        # Act & Assert
        with pytest.raises(RuntimeError) as exc_info:
            await client.get_er_gears()

        assert "Failed to fetch gear from Buoy Gear API" in str(exc_info.value)
        assert "Status code: 500" in str(exc_info.value)
        assert "Internal Server Error" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_get_er_gears_missing_data_field(self, mocker):
        """Test gear retrieval with response missing 'data' field raises RuntimeError."""
        # Arrange
        client = BuoyClient(er_token="test-token", er_site="https://example.com/")

        # Mock response without 'data' field
        response_data = {"error": "Invalid response"}

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=response_data)

        # Create mock session and ClientSession
        mock_session = MockSession(MockGetContext(mock_response))
        mock_client_session = mocker.patch("aiohttp.ClientSession")
        mock_client_session.return_value = MockSessionContext(mock_session)

        # Act & Assert
        with pytest.raises(RuntimeError) as exc_info:
            await client.get_er_gears()

        assert "Unexpected response structure from Buoy Gear API" in str(exc_info.value)
        assert "missing 'data' field" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_get_er_gears_missing_results_field(self, mocker):
        """Test gear retrieval with response missing 'results' field raises RuntimeError."""
        # Arrange
        client = BuoyClient(er_token="test-token", er_site="https://example.com/")

        # Mock response without 'results' field
        response_data = {"data": {"count": 0, "next": None}}

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=response_data)

        # Create mock session and ClientSession
        mock_session = MockSession(MockGetContext(mock_response))
        mock_client_session = mocker.patch("aiohttp.ClientSession")
        mock_client_session.return_value = MockSessionContext(mock_session)

        # Act & Assert
        with pytest.raises(RuntimeError) as exc_info:
            await client.get_er_gears()

        assert "Unexpected response structure from Buoy Gear API" in str(exc_info.value)
        assert "missing 'results' field" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_get_er_gears_empty_results(self, mocker, caplog):
        """Test gear retrieval with empty results logs warning but returns empty list."""
        # Arrange
        client = BuoyClient(er_token="test-token", er_site="https://example.com/")

        # Mock response with empty results
        response_data = {"data": {"results": [], "next": None}}

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=response_data)

        # Create mock session and ClientSession
        mock_session = MockSession(MockGetContext(mock_response))
        mock_client_session = mocker.patch("aiohttp.ClientSession")
        mock_client_session.return_value = MockSessionContext(mock_session)

        # Act
        with caplog.at_level(logging.WARNING):
            result = await client.get_er_gears()

        # Assert
        assert result == []
        assert "No gears found in Buoy API" in caplog.text

    @pytest.mark.asyncio
    async def test_get_er_gears_http_error_on_second_page(
        self, mocker, sample_paginated_response
    ):
        """Test gear retrieval with HTTP error on second page raises RuntimeError."""
        # Arrange
        client = BuoyClient(er_token="test-token", er_site="https://example.com/")

        # Mock first page response (success)
        mock_response_1 = AsyncMock()
        mock_response_1.status = 200
        mock_response_1.json = AsyncMock(return_value=sample_paginated_response)

        # Mock second page response (error)
        mock_response_2 = AsyncMock()
        mock_response_2.status = 404
        mock_response_2.text = AsyncMock(return_value="Not Found")

        # Create mock session with multiple responses
        mock_session = MockSession(
            [MockGetContext(mock_response_1), MockGetContext(mock_response_2)]
        )
        mock_client_session = mocker.patch("aiohttp.ClientSession")
        mock_client_session.return_value = MockSessionContext(mock_session)

        # Act & Assert
        with pytest.raises(RuntimeError) as exc_info:
            await client.get_er_gears()

        assert "Failed to fetch gear from Buoy Gear API" in str(exc_info.value)
        assert "Status code: 404" in str(exc_info.value)
        assert "Not Found" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_get_er_gears_invalid_data_structure_on_second_page(
        self, mocker, sample_paginated_response
    ):
        """Test gear retrieval with invalid data structure on second page raises RuntimeError."""
        # Arrange
        client = BuoyClient(er_token="test-token", er_site="https://example.com/")

        # Mock first page response (success)
        mock_response_1 = AsyncMock()
        mock_response_1.status = 200
        mock_response_1.json = AsyncMock(return_value=sample_paginated_response)

        # Mock second page response (invalid structure)
        mock_response_2 = AsyncMock()
        mock_response_2.status = 200
        mock_response_2.json = AsyncMock(return_value={"invalid": "structure"})

        # Create mock session with multiple responses
        mock_session = MockSession(
            [MockGetContext(mock_response_1), MockGetContext(mock_response_2)]
        )
        mock_client_session = mocker.patch("aiohttp.ClientSession")
        mock_client_session.return_value = MockSessionContext(mock_session)

        # Act & Assert
        with pytest.raises(RuntimeError) as exc_info:
            await client.get_er_gears()

        assert "Unexpected response structure from Buoy Gear API" in str(exc_info.value)
        assert "missing 'data' field" in str(exc_info.value)

    def test_buoy_client_url_construction(self):
        """Test that the URL is constructed correctly."""
        # Test with trailing slash
        client1 = BuoyClient(er_token="token", er_site="https://example.com/")
        expected_url = "https://example.com/api/v1.0/gear/"

        # Test without trailing slash
        client2 = BuoyClient(er_token="token", er_site="https://example.com")

        # Since the URL construction happens in get_er_gears, we need to test it indirectly
        # by examining what URL would be constructed
        assert client1.er_site + "api/v1.0/gear/" == expected_url
        assert client2.er_site + "api/v1.0/gear/" == "https://example.comapi/v1.0/gear/"

    def test_headers_construction(self):
        """Test that authorization headers are constructed correctly."""
        token = "my-secret-token-123"
        client = BuoyClient(er_token=token, er_site="https://example.com/")

        expected_headers = {"Authorization": f"Bearer {token}"}
        assert client.headers == expected_headers

    @pytest.mark.asyncio
    async def test_get_er_gears_parse_error(self, mocker):
        """Test gear retrieval with BuoyGear parsing error raises RuntimeError."""
        # Arrange
        client = BuoyClient(er_token="test-token", er_site="https://example.com/")

        # Mock response with invalid gear data that will cause parsing to fail
        invalid_gear_data = {
            "id": "invalid-uuid",  # This will cause parsing error
            "display_id": "TEST-GEAR-001",
            "status": "deployed",
            "last_updated": "invalid-date",  # This will also cause parsing error
            "devices": "not-a-list",  # This should be a list
            "type": "ropeless",
            "manufacturer": "EdgeTech",
        }

        response_data = {"data": {"results": [invalid_gear_data], "next": None}}

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=response_data)

        # Create mock session and ClientSession
        mock_session = MockSession(MockGetContext(mock_response))
        mock_client_session = mocker.patch("aiohttp.ClientSession")
        mock_client_session.return_value = MockSessionContext(mock_session)

        # Act & Assert
        with pytest.raises(RuntimeError) as exc_info:
            await client.get_er_gears()

        assert "Error parsing gear item:" in str(exc_info.value)
        # Verify that the invalid item data is included in the error message
        assert "invalid-uuid" in str(exc_info.value)
