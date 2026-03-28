# EdgeTech Integration Documentation

## Overview

This document describes the integration between EdgeTech's Trap Tracker API and our buoy tracking system (Earth Ranger destinations). It focuses on:

1. **EdgeTech API Connection** - Authentication and data retrieval mechanisms
2. **Data Structure** - Expected format from EdgeTech
3. **Record Filtering** - Which buoy records we process vs. discard
4. **Observation Mapping** - How EdgeTech data transforms into our observation format

The integration runs automatically every 3 minutes via a scheduled task and syncs the last 90 days of buoy data.

---

## Development

### Create Python Environment

```bash
uv venv --python=3.10 .venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

### pyjq setup on macos
```
brew update
brew install autoconf automake libtool pkg-config
```

verify installed:
```
which autoreconf
autoreconf --version
```

ensure homebrew in path
```
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zshrc
eval "$(/opt/homebrew/bin/brew shellenv)"
```

set compile flags
```
export CFLAGS="-I/opt/homebrew/include"
export LDFLAGS="-L/opt/homebrew/lib"
```

```
uv pip install pyjq
```


## EdgeTech API Connection

### Authentication

**OAuth2 Flow**
- **Provider**: Auth0 (EdgeTech)
- **Token Endpoint**: `https://trap-tracker.auth0.com/oauth/token`
- **Grant Type**: `refresh_token`
- **Required Scopes**: `offline_access database:dump openid profile email`

**Configuration**
```python
EdgeTechAuthConfiguration:
  - token_json: OAuth token JSON (contains access_token, refresh_token, expires_at)
  - client_id: EdgeTech API client identifier
```

**Token Management**
- Tokens expire after a set period (tracked via `expires_at` timestamp)
- Automatic refresh occurs when `current_time >= expires_at`
- Refresh uses the `refresh_token` from the original token JSON

**Refresh Request**
```python
POST https://trap-tracker.auth0.com/oauth/token
{
  "client_id": "<client_id>",
  "refresh_token": "<refresh_token>",
  "redirect_uri": "https://app.local",
  "scope": "offline_access database:dump openid profile email",
  "grant_type": "refresh_token"
}
```

### Data Retrieval Process

EdgeTech provides buoy data through a **database dump mechanism** that involves multiple steps:

#### Step 1: Initiate Database Dump
```python
POST {api_base_url}/v1/database-dump/tasks
Headers:
  Authorization: Bearer <access_token>

Response: 303 See Other
Location: /v1/database-dump/tasks/{task_id}
```

#### Step 2: Poll for Completion
```python
GET {api_base_url}/v1/database-dump/tasks/{task_id}
Headers:
  Authorization: Bearer <access_token>

Response (while processing): 200 OK
Response (when ready): 303 See Other
Location: <download_url>
```

**Polling Configuration**
- Maximum attempts: 60 (configurable via `num_get_retry`)
- Retry interval: 1 second between attempts
- Total max wait time: ~60 seconds

#### Step 3: Download Data
```python
GET <download_url>

Response Headers:
  Content-Disposition: attachment; filename="database-dump-{timestamp}.json.gz"

Response Body: GZIP-compressed JSON
```

#### Step 4: Process Data
1. Download GZIP file
2. Decompress in-memory
3. Parse JSON array
4. Convert each item to `Buoy` object
5. Apply temporal filters (optional `start_datetime`)

**Download Implementation**
```python
async def download_data(start_datetime: Optional[datetime]) -> List[Buoy]:
    # 1. Get valid token (refresh if needed)
    token = await self.get_token()

    # 2. Initiate dump
    # 3. Poll until ready
    # 4. Download GZIP file
    # 5. Decompress and parse JSON
    # 6. Filter by start_datetime if provided

    return buoys
```

---

## EdgeTech Data Structure

### Buoy Record Format

EdgeTech provides buoy data as a JSON array. Each element represents a buoy with its complete state and change history.

**Top-Level Structure**
```json
{
  "currentState": { /* CurrentState object */ },
  "serialNumber": "ET-12345",
  "userId": "user@example.com",
  "changeRecords": [ /* Array of ChangeRecord objects */ ]
}
```

### CurrentState Object

The `currentState` contains all current information about a buoy:

```json
{
  "etag": "\"abc123def456\"",
  "isDeleted": false,
  "serialNumber": "ET-12345",
  "releaseCommand": "RELEASE_CMD",
  "statusCommand": "STATUS_CMD",
  "idCommand": "ID_CMD",
  "isNfcTag": false,
  "modelNumber": "ET-2000",
  "dateOfManufacture": "2024-01-15T00:00:00Z",
  "dateOfBatteryChange": "2024-06-01T00:00:00Z",
  "dateDeployed": "2025-10-01T08:30:00Z",
  "isDeployed": true,
  "dateRecovered": null,
  "recoveredLatDeg": null,
  "recoveredLonDeg": null,
  "recoveredRangeM": null,
  "dateStatus": "2025-10-20T10:00:00Z",
  "statusRangeM": 150.5,
  "statusIsTilted": false,
  "statusBatterySoC": 85,
  "lastUpdated": "2025-10-20T10:00:00Z",
  "latDeg": 42.123456,
  "lonDeg": -70.654321,
  "endLatDeg": 42.234567,
  "endLonDeg": -70.765432,
  "isTwoUnitLine": true,
  "endUnit": "ET-12346",
  "startUnit": null
}
```

**Key Fields**

| Field | Type | Description |
|-------|------|-------------|
| `etag` | string | Version identifier (quotes stripped during parsing) |
| `isDeleted` | boolean | Whether the buoy has been deleted from the system |
| `serialNumber` | string | Unique buoy identifier |
| `isDeployed` | boolean | Whether the buoy is currently deployed in water |
| `lastUpdated` | datetime | Last modification timestamp (ISO 8601) |
| `latDeg` / `lonDeg` | float? | Deployment location (start point) |
| `endLatDeg` / `endLonDeg` | float? | End location (for two-unit lines) |
| `recoveredLatDeg` / `recoveredLonDeg` | float? | Recovery location |
| `isTwoUnitLine` | boolean? | Whether this is a two-unit line system |
| `endUnit` | string? | Serial number of the end unit buoy |
| `startUnit` | string? | Serial number of the start unit buoy |
| `statusBatterySoC` | int? | Battery state of charge (0-100%) |
| `statusIsTilted` | boolean? | Whether the buoy is tilted |

**Location Hierarchy**
1. **Deployed Location**: `latDeg` / `lonDeg` (primary deployment point)
2. **End Location**: `endLatDeg` / `endLonDeg` (for two-unit systems)
3. **Recovery Location**: `recoveredLatDeg` / `recoveredLonDeg` (when retrieved)

### Two-Unit Line Systems

EdgeTech supports buoy systems with two physical units connected by a line:

**Start Unit Record**
```json
{
  "serialNumber": "ET-12345",
  "currentState": {
    "latDeg": 42.123456,
    "lonDeg": -70.654321,
    "isTwoUnitLine": true,
    "endUnit": "ET-12346",
    "startUnit": null
  }
}
```

**End Unit Record**
```json
{
  "serialNumber": "ET-12346",
  "currentState": {
    "latDeg": 42.234567,
    "lonDeg": -70.765432,
    "isTwoUnitLine": true,
    "endUnit": null,
    "startUnit": "ET-12345"
  }
}
```

**Processing Logic**
- Both units share the same `userId`
- The start unit (`startUnit: null`) initiates processing
- The end unit (`endUnit: null`) is skipped (processed as part of start unit)
- If end unit is missing, the start unit is skipped with a warning
- **Circular two-unit protection**: If the same two devices are each configured as start with the other as end (e.g. A.endUnit=B and B.endUnit=A), only one gearset is created. The duplicate start is skipped (canonical lead is the device with the smaller serial number), and a warning is logged.

---

## Record Filtering

### Records We Process

✅ **Included Records**
- `isDeleted: false` - Active buoys in the system
- `isDeployed: true` - Currently deployed in water
- Has valid location data (at least one of):
  - `latDeg` AND `lonDeg` (deployment location)
  - `endLatDeg` AND `endLonDeg` (two-unit end location)
  - `recoveredLatDeg` AND `recoveredLonDeg` (recovery location)

✅ **Additional Processing Rules**
- Only the **most recent state** per buoy (grouped by `serialNumber` + `userId`)
  - Key: `{serialNumber}/{hashedUserId}`
  - Comparison: Latest `lastUpdated` timestamp wins
- Temporal filter applied if configured:
  - Only buoys with `lastUpdated > start_datetime`
  - Default: Last 90 days of data

### Records We Discard

❌ **Excluded Records**

| Condition | Reason | Log Level |
|-----------|--------|-----------|
| `isDeleted: true` | Buoy removed from system | WARNING |
| `isDeployed: false` | Buoy not in water | WARNING |
| No location data | Cannot track position | WARNING |
| Older duplicate | Superseded by newer state | (Implicit filtering) |

**Filter Implementation**
```python
def _should_skip_buoy(record: Buoy) -> Tuple[bool, Optional[str]]:
    if record.currentState.isDeleted:
        return True, f"Deleted buoy {record.serialNumber}"

    if not record.currentState.isDeployed:
        return True, f"Not deployed {record.serialNumber}"

    if not record.has_location:
        return True, f"No location data {record.serialNumber}"

    return False, None
```

**Location Validation**
```python
@property
def has_location(self) -> bool:
    has_deployed = (self.latDeg is not None and self.lonDeg is not None)
    has_end = (self.endLatDeg is not None and self.endLonDeg is not None)
    has_recovered = (self.recoveredLatDeg is not None and self.recoveredLonDeg is not None)

    return has_deployed or has_end or has_recovered
```

### Deduplication Strategy

Since serial numbers can be duplicated across users, we group by the combination of serial number AND user:

**Step 1: Group by Buoy Identity**
```python
key = f"{serialNumber}/{hashed_user_id}"
# Example: "8899CEDAAA/a1b2c3d4"
```

This ensures that if two different users have entered the same serial number, they are treated as **separate buoys**.

**Step 2: Select Latest State**
```python
if record.currentState.lastUpdated > existing.currentState.lastUpdated:
    latest[key] = record
```

**Result**: One record per unique buoy (serial + user combination)

---

## Change Detection and Operations

After filtering, we compare EdgeTech data with our existing Earth Ranger records to determine required actions:

### Operation Categories

**1. DEPLOY (New Deployments)**
- Buoy exists in EdgeTech but **not** in Earth Ranger
- Checked device IDs (matched by `mfr_device_id` in ER gears):
  - Primary: `{serialNumber}_{hashedUserId}_A`
  - Standard: `{serialNumber}_{hashedUserId}`
- Action: Create deployment gear payload with new UUID as `set_id`

**2. RE-DEPLOY (Same units, new deployment)**
- Buoy exists in **both** EdgeTech and Earth Ranger, and ER gear is still `status: "deployed"`
- EdgeTech **`dateDeployed`** is **more than 1 minute after** the ER gear’s deployment time (so we treat it as a new deployment, not an update)
- Action: **Haul** the existing gear (close it), then **Deploy** a new gear set with a new `set_id`
- **Processing order**: Haul payload is sent first, then the new deployment payload, so the previous gear is closed before the new one is created
- **Why**: When the same serial(s) are deployed again (e.g. same two-unit pair with a new `dateDeployed`), we close the previous deployment in ER/Buoy and create a new one instead of updating the old gear in place
- **Haul timestamp for re-deployments**: The haul payload’s `recorded_at` must not collide with the deploy payload’s `recorded_at` (which uses `dateDeployed`), since both share the same `device_id` and ER enforces a `(device_id, recorded_at)` unique constraint. Timestamps are truncated to seconds (`_remove_milliseconds`), so values within the same second will collide.
  - **With `dateRecovered` in `changeRecords`**: When a buoy is hauled and redeployed within seconds, `currentState.dateRecovered` is cleared by the redeploy. The haul `recorded_at` is sourced from the most recent `dateRecovered` in `changeRecords`. Recovery location (`recoveredLatDeg`/`recoveredLonDeg`) is also recovered from `changeRecords`.
  - **Without `dateRecovered` (skipped haul)**: EdgeTech may skip the haul stage entirely — the gear goes straight from one deployment to another with no `dateRecovered` anywhere. In this case, `lastUpdated` and `dateDeployed` are typically within the same second (both reflect the new deployment), so the haul uses `dateDeployed - 1 second` as a deterministic `recorded_at` that is guaranteed not to collide with the deploy payload.

**2a. DEPLOY (Recovery from missed deployment)**
- Buoy exists in **both** EdgeTech and Earth Ranger, but ER gear `status` is **not** `"deployed"` (e.g. `"hauled"`)
- EdgeTech shows `isDeployed: true`
- **Why**: This handles the case where a previous re-deployment haul succeeded but the deploy failed (e.g. `recorded_at` collision). On subsequent processor runs, the hauled ER gear + deployed EdgeTech state is recognized as a missed deployment.
- Action: Create deployment gear payload with new UUID as `set_id` (no haul needed since gear is already hauled)

**3. UPDATE (Location Changes)**
- Buoy exists in both systems
- Location has changed (primary device coordinates differ from ER)
- Buoy still marked as `isDeployed: true` and `isDeleted: false`
- **Not** a re-deployment (EdgeTech `dateDeployed` is not more than 1 minute after ER gear’s deployment)
- **Note**: Buoys are initially identified for update when `lastUpdated > last_updated` OR location changed, but during payload generation, updates are only sent when the location has actually changed. Metadata-only updates (newer timestamp, same location) are skipped.
- Action: Create update gear payload using existing ER gear’s `set_id`

**4. HAUL (Retrievals)**
- Buoy exists in both systems, but EdgeTech **explicitly** marks it as:
  - `isDeleted: true`, OR
  - `isDeployed: false`
- Earth Ranger gear must still show `status: "deployed"`
- **Important**: Absence from EdgeTech data does NOT trigger a haul
- Action: Create haul gear payload using existing ER gear's `set_id`

**5. NO-OP (Skip)**
- Buoy exists in both systems
- No location change detected
- Same `lastUpdated` timestamp
- Action: None (logged and skipped)

---

## Set ID Resolution

When processing EdgeTech buoys, the system must determine whether to create a new gear set or update an existing one. This is done by matching against existing EarthRanger gears.

### Lookup Process

**Step 1: Fetch Existing ER Gears**
```python
er_gears = await er_client.get_er_gears()  # GET /api/v1.0/gear/
# Filter: manufacturer.lower() == "edgetech"
```

**Step 2: Build Device ID Mapping**
```python
er_gears_devices_id_to_gear = {
    device.mfr_device_id: gear
    for gear in er_gears
    for device in gear.devices
}
# Example mapping:
# "8899CEDAAA_a1b2c3d4_A" -> BuoyGear(id=UUID(...), display_id="...", ...)
# "8899CEDAAA_a1b2c3d4_B" -> BuoyGear(id=UUID(...), display_id="...", ...)
```

**Step 3: Match EdgeTech Buoy to ER Gear**
```python
# For each EdgeTech buoy, construct lookup keys:
primary_key = f"{serial_number}_{hashed_user_id}_A"
standard_key = f"{serial_number}_{hashed_user_id}"

# Look up in mapping (try primary first, then standard)
er_gear = er_gears_devices_id_to_gear.get(primary_key) \
       or er_gears_devices_id_to_gear.get(standard_key)
```

### Set ID Determination

| Scenario | ER Gear Found? | set_id Source |
|----------|----------------|---------------|
| **New Deployment** | No | Generate new UUID: `str(uuid4())` |
| **Re-deployment** | Yes | Haul: use ER gear's `display_id`. Deploy: generate new UUID |
| **Update Existing** | Yes | Use ER gear's `id` field (UUID) |
| **Haul Existing** | Yes | Use ER gear's `display_id` field |

**Example Flow**:
```
EdgeTech Buoy:
  serialNumber: "8899CEDAAA"
  userId: "634431265e87a0a75163a20b"

Hashed User ID: a1b2c3d4
Lookup Key: "8899CEDAAA_a1b2c3d4_A"

Case 1 - Not in ER:
  → Generate set_id: "550e8400-e29b-41d4-a716-446655440000"
  → Create new gear set

Case 2 - Found in ER with id="abc-123-def":
  → Use set_id: "abc-123-def"
  → Update existing gear set
```

### Source ID Mapping

For device-level tracking, the system also maintains a mapping from manufacturer device IDs to ER source IDs:

```python
sources = await er_client.get_sources()  # GET /api/v1.0/sources/
manufacturer_id_to_source_id = {
    source["manufacturer_id"]: source["id"]
    for source in sources
}
```

This ensures that when updating existing gear sets, the correct source IDs are preserved rather than generating new ones.

---

## Gear Payload Mapping

### Gear Payload Schema

We transform EdgeTech buoy data into gear payloads for the EarthRanger Buoy API (`POST /api/v1.0/gear/`):

```json
{
  "set_id": "<uuid>",
  "owner_id": "<user_email>",
  "manufacturer_name": "EdgeTech",
  "deployment_type": "single" | "trawl",
  "devices_in_set": <number>,
  "initial_deployment_date": "<iso8601_timestamp>",
  "devices": [
    {
      "device_id": "<uuid>",
      "mfr_device_id": "<manufacturer_device_id>",
      "last_deployed": "<iso8601_timestamp>",
      "last_updated": "<iso8601_timestamp>",
      "recorded_at": "<iso8601_timestamp>",
      "device_status": "deployed" | "hauled",
      "location": {
        "latitude": <float>,
        "longitude": <float>
      },
      "device_additional_data": { /* Original EdgeTech data */ }
    }
  ]
}
```

### Field Mapping

| Gear Payload Field | EdgeTech Source | Notes |
|-------------------|----------------|-------|
| `set_id` | Generated or ER lookup | New UUID for deploy, existing ID for update/haul |
| `owner_id` | `userId` | Original user email from EdgeTech |
| `manufacturer_name` | Static | Always `"EdgeTech"` |
| `deployment_type` | Derived | `"single"` (1 device) or `"trawl"` (2 devices) |
| `devices_in_set` | Derived | Count of devices in the gear set |
| `initial_deployment_date` | `currentState.dateDeployed` | Only included for new deployments |
| `device_id` | ER lookup or generated | Existing source ID or new UUID |
| `mfr_device_id` | Derived | `{serialNumber}_{hashedUserId}[_A/B]` |
| `last_deployed` | `currentState.dateDeployed` | Falls back to `lastUpdated` |
| `last_updated` | `currentState.lastUpdated` | Microseconds removed |
| `recorded_at` | `currentState.dateDeployed` | Timestamp of the event |
| `device_status` | Derived | `"deployed"` or `"hauled"` |
| `location.latitude` | `currentState.latDeg` or `endLatDeg` | Depends on device position |
| `location.longitude` | `currentState.lonDeg` or `endLonDeg` | Depends on device position |
| `device_additional_data` | `currentState` | Complete state (minus `changeRecords`) |

### Device ID Generation

Device IDs uniquely identify each tracking device within a buoy system.

#### Why Serial Number Alone Is Not Unique

In EdgeTech's system, `serialNumber` is a user-entered field - fishermen manually input their buoy serial numbers. This means:

1. **Serial numbers are NOT globally unique** - Two different users could enter the same serial number (intentionally or by mistake)
2. **The true unique key is `serialNumber + userId`** - EdgeTech scopes each buoy record to the user who entered it

**Example from EdgeTech data**:
```json
{
  "serialNumber": "8899CEDAAA",      // User-entered, potentially duplicated
  "userId": "634431265e87a0a75163a20b",  // EdgeTech account ID, globally unique
  "currentState": { ... }
}
```

To create a globally unique device identifier for EarthRanger, we combine both fields into `mfr_device_id`.

#### User ID Hashing

**Purpose**: Anonymize user identifiers while maintaining uniqueness (we don't want to expose raw EdgeTech account IDs)

```python
def get_hashed_user_id(user_id: str) -> str:
    # Convert to hex
    user_id_hex = user_id.encode("utf-8").hex()

    # Hash with Hashids (min length: 8)
    hashids = Hashids(min_length=8)
    hashed = hashids.encode_hex(user_id_hex)

    return hashed  # e.g., "a1b2c3d4"
```

#### Single-Unit Buoys

For standard buoys (not two-unit lines), we create **two devices** representing start and end of the trap line:

```
Device A: {serialNumber}_{hashedUserId}_A
Device B: {serialNumber}_{hashedUserId}_B
```

**Example**:
```
EdgeTech Data:
  serialNumber: "8899CEDAAA"
  userId: "634431265e87a0a75163a20b"

Hashed User ID: a1b2c3d4

Device A: 8899CEDAAA_a1b2c3d4_A (start point - uses latDeg/lonDeg)
Device B: 8899CEDAAA_a1b2c3d4_B (end point - uses endLatDeg/endLonDeg)
```

**Gear Payload Notes**:
- Both devices share the same `set_id`
- Device A uses `latDeg` / `lonDeg`
- Device B uses `endLatDeg` / `endLonDeg` (if present)
- If end coordinates missing, only Device A is created (deployment_type="single")

#### Two-Unit Line Buoys

For two-unit systems (`isTwoUnitLine: true`), each physical buoy becomes a separate device (no `_A`/`_B` suffix):

```
Start Device: {startSerialNumber}_{hashedUserId}
End Device: {endSerialNumber}_{hashedUserId}
```

**Example**:
```
Start Buoy:
  serialNumber: "8899CEDAAA"
  endUnit: "7788BCDEAA"
  startUnit: null

End Buoy:
  serialNumber: "7788BCDEAA"
  endUnit: null
  startUnit: "8899CEDAAA"

userId (both): "634431265e87a0a75163a20b"
Hashed User ID: a1b2c3d4

Start Device: 8899CEDAAA_a1b2c3d4
End Device: 7788BCDEAA_a1b2c3d4
```

**Gear Payload Notes**:
- Both devices share the same `set_id`
- Start device uses start buoy's `latDeg` / `lonDeg`
- End device uses end buoy's `latDeg` / `lonDeg`
- Processing triggered only by start unit record (end unit records are skipped)

### Device Status Values

#### deployed

Set when:
- New buoy appears in EdgeTech (DEPLOY operation)
- Buoy location updated in EdgeTech (UPDATE operation)

**Indicates**: Buoy is actively deployed in water

```json
{
  "device_status": "deployed"
}
```

#### hauled

Set when:
- EdgeTech marks buoy as `isDeleted: true` or `isDeployed: false`
- Buoy has `dateRecovered` set

**Indicates**: Buoy has been recovered from water

```json
{
  "device_status": "hauled"
}
```

---

## Gear Payload Examples

### Example 1: Single-Unit Deployment

**EdgeTech Input**:
```json
{
  "serialNumber": "8899CEDAAA",
  "userId": "634431265e87a0a75163a20b",
  "currentState": {
    "serialNumber": "8899CEDAAA",
    "isDeleted": false,
    "isDeployed": true,
    "dateDeployed": "2026-01-11T21:40:16.781Z",
    "lastUpdated": "2026-01-11T21:40:17.039Z",
    "latDeg": 44.3141283,
    "lonDeg": -68.31271,
    "endLatDeg": 44.31517,
    "endLonDeg": -68.31224,
    "isTwoUnitLine": false
  }
}
```

**Our Gear Payload**:
```json
{
  "set_id": "550e8400-e29b-41d4-a716-446655440000",
  "owner_id": "634431265e87a0a75163a20b",
  "manufacturer_name": "EdgeTech",
  "deployment_type": "trawl",
  "devices_in_set": 2,
  "initial_deployment_date": "2026-01-11T21:40:16",
  "devices": [
    {
      "device_id": "uuid-for-device-a",
      "mfr_device_id": "8899CEDAAA_a1b2c3d4_A",
      "device_status": "deployed",
      "last_deployed": "2026-01-11T21:40:16",
      "last_updated": "2026-01-11T21:40:17",
      "recorded_at": "2026-01-11T21:40:16",
      "location": {
        "latitude": 44.3141283,
        "longitude": -68.31271
      }
    },
    {
      "device_id": "uuid-for-device-b",
      "mfr_device_id": "8899CEDAAA_a1b2c3d4_B",
      "device_status": "deployed",
      "last_deployed": "2026-01-11T21:40:16",
      "last_updated": "2026-01-11T21:40:17",
      "recorded_at": "2026-01-11T21:40:16",
      "location": {
        "latitude": 44.31517,
        "longitude": -68.31224
      }
    }
  ]
}
```

### Example 2: Two-Unit Deployment

**EdgeTech Input** (Start Unit - identified by `startUnit: null`):
```json
{
  "serialNumber": "8899CEDAAA",
  "userId": "634431265e87a0a75163a20b",
  "currentState": {
    "serialNumber": "8899CEDAAA",
    "isDeleted": false,
    "isDeployed": true,
    "dateDeployed": "2026-01-11T21:40:16.781Z",
    "lastUpdated": "2026-01-11T21:40:17.039Z",
    "latDeg": 44.3141283,
    "lonDeg": -68.31271,
    "isTwoUnitLine": true,
    "endUnit": "7788BCDEAA",
    "startUnit": null
  }
}
```

**EdgeTech Input** (End Unit - identified by `endUnit: null`):
```json
{
  "serialNumber": "7788BCDEAA",
  "userId": "634431265e87a0a75163a20b",
  "currentState": {
    "serialNumber": "7788BCDEAA",
    "isDeleted": false,
    "isDeployed": true,
    "dateDeployed": "2026-01-11T21:40:16.781Z",
    "lastUpdated": "2026-01-11T21:40:17.039Z",
    "latDeg": 44.31517,
    "lonDeg": -68.31224,
    "isTwoUnitLine": true,
    "endUnit": null,
    "startUnit": "8899CEDAAA"
  }
}
```

**Our Gear Payload** (2 devices from different physical buoys):
```json
{
  "set_id": "550e8400-e29b-41d4-a716-446655440000",
  "owner_id": "634431265e87a0a75163a20b",
  "manufacturer_name": "EdgeTech",
  "deployment_type": "trawl",
  "devices_in_set": 2,
  "initial_deployment_date": "2026-01-11T21:40:16",
  "devices": [
    {
      "device_id": "uuid-for-start-device",
      "mfr_device_id": "8899CEDAAA_a1b2c3d4",
      "device_status": "deployed",
      "last_deployed": "2026-01-11T21:40:16",
      "last_updated": "2026-01-11T21:40:17",
      "recorded_at": "2026-01-11T21:40:16",
      "location": {
        "latitude": 44.3141283,
        "longitude": -68.31271
      }
    },
    {
      "device_id": "uuid-for-end-device",
      "mfr_device_id": "7788BCDEAA_a1b2c3d4",
      "device_status": "deployed",
      "last_deployed": "2026-01-11T21:40:16",
      "last_updated": "2026-01-11T21:40:17",
      "recorded_at": "2026-01-11T21:40:16",
      "location": {
        "latitude": 44.31517,
        "longitude": -68.31224
      }
    }
  ]
}
```

**Note**: Two-unit lines use the actual serial numbers of each buoy (no `_A`/`_B` suffix) since each physical unit has its own serial number. Processing is triggered only by the start unit record (`startUnit: null`).

### Example 3: Buoy Retrieval (Haul)

When EdgeTech explicitly marks a buoy as `isDeleted: true` or `isDeployed: false`, we create a haul payload using Earth Ranger's existing record.

**Trigger Condition** (EdgeTech data showing recovery):
```json
{
  "serialNumber": "8899CEDAAA",
  "userId": "634431265e87a0a75163a20b",
  "currentState": {
    "serialNumber": "8899CEDAAA",
    "isDeleted": false,
    "isDeployed": false,
    "dateRecovered": "2026-01-11T21:34:58.923Z",
    "lastUpdated": "2026-01-11T21:35:02.733Z",
    "recoveredLatDeg": 44.3124035,
    "recoveredLonDeg": -68.3037248,
    "recoveredRangeM": 88.382,
    "latDeg": null,
    "lonDeg": null
  }
}
```

**Earth Ranger Existing Gear** (still shows as deployed):
```json
{
  "id": "abc-123-def",
  "display_id": "abc-123-def",
  "status": "deployed",
  "devices": [
    {
      "mfr_device_id": "8899CEDAAA_a1b2c3d4_A",
      "location": { "latitude": 44.3141283, "longitude": -68.31271 }
    },
    {
      "mfr_device_id": "8899CEDAAA_a1b2c3d4_B",
      "location": { "latitude": 44.31517, "longitude": -68.31224 }
    }
  ]
}
```

**Our Haul Payload**:
```json
{
  "set_id": "abc-123-def",
  "manufacturer_name": "EdgeTech",
  "deployment_type": "trawl",
  "devices": [
    {
      "device_id": "existing-uuid-a",
      "mfr_device_id": "8899CEDAAA_a1b2c3d4_A",
      "device_status": "hauled",
      "recorded_at": "2026-01-11T21:34:58",
      "location": {
        "latitude": 44.3124035,
        "longitude": -68.3037248
      }
    },
    {
      "device_id": "existing-uuid-b",
      "mfr_device_id": "8899CEDAAA_a1b2c3d4_B",
      "device_status": "hauled",
      "recorded_at": "2026-01-11T21:34:58",
      "location": {
        "latitude": 44.3124035,
        "longitude": -68.3037248
      }
    }
  ]
}
```

**Location Priority for Hauls**:
1. Recovery location from EdgeTech `currentState` (`recoveredLatDeg`/`recoveredLonDeg`) if available
2. Recovery location from EdgeTech `changeRecords` (for re-deployments where `currentState` was overwritten)
3. Fallback to last deployed location from Earth Ranger

**Note**: All devices in the gear set use the same recovery location since there's only one recovery point.

---

## Data Quality and Edge Cases

### Handling Missing Data

| Missing Field | Behavior |
|--------------|----------|
| `latDeg` / `lonDeg` | Buoy filtered out (no location) |
| `endLatDeg` / `endLonDeg` | Single observation created (only Device A) |
| End unit in two-unit system | Start unit skipped with warning |
| `lastUpdated` | Required field - parsing would fail |

### Timestamp Handling

**Input Format**: ISO 8601 with timezone
```
2025-10-20T10:00:00.123456Z
```

**Normalization**:
```python
recorded_at = state.lastUpdated.replace(microsecond=0)
# Result: 2025-10-20T10:00:00Z
```

**Purpose**: Ensure consistent timestamp comparison and prevent false positives in change detection

### Location Change Detection

We skip UPDATE operations if location hasn't changed:

```python
if (er_device_lat == edgetech_buoy_lat and
    er_device_long == edgetech_buoy_long):
    # No change - skip update
    continue
```

**Comparison**: Direct float equality (no tolerance threshold)

### Two-Unit Coordination

**Requirements**:
- Both start and end units must exist in EdgeTech data
- Both must have same `userId`
- End unit lookup: `f"{endUnitSerial}/{hashedUserId}"`

**Failure Modes**:
```python
if not end_unit_buoy:
    logger.warning(f"End unit {endUnit} not found for {serialNumber}, skipping")
    continue
```

Result: Entire system skipped if either unit missing

---

## Processing Flow Summary

```
┌─────────────────────────────────────────────────────────────┐
│ 1. DOWNLOAD FROM EDGETECH                                   │
│    - OAuth token refresh                                    │
│    - Database dump request                                  │
│    - Poll until ready                                       │
│    - Download GZIP                                          │
│    - Parse JSON → List[Buoy]                                │
└────────────────┬────────────────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────────────────┐
│ 2. FILTER EDGETECH DATA                                     │
│    ✓ isDeleted = false                                      │
│    ✓ isDeployed = true                                      │
│    ✓ Has location data                                      │
│    ✓ Most recent per serial/user                            │
│    ✓ Within time window (90 days)                           │
└────────────────┬────────────────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────────────────┐
│ 3. FETCH EARTH RANGER GEARS                                 │
│    - GET /api/v1.0/gear/                                    │
│    - Filter manufacturer = "edgetech"                       │
│    - Map by device_id                                       │
└────────────────┬────────────────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────────────────┐
│ 4. IDENTIFY OPERATIONS                                      │
│    DEPLOY:   In EdgeTech (deployed), not in ER              │
│    RE-DEPLOY: In both, EdgeTech dateDeployed > ER + 1 min   │
│               → Haul existing gear, then deploy new          │
│    RECOVERY: ER gear hauled but EdgeTech isDeployed=true     │
│               → Deploy new gear (haul already done)          │
│    UPDATE:   In both, EdgeTech newer + location changed      │
│    HAUL:     In both, EdgeTech isDeleted/!isDeployed         │
│    (Absence from EdgeTech does NOT trigger haul)             │
└────────────────┬────────────────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────────────────┐
│ 5. GENERATE GEAR PAYLOADS (order matters)                   │
│    a) Hauls first (close existing gears)                    │
│    b) Deployments (new gear sets)                           │
│    c) Updates (location/status changes)                    │
│    For each: set_id, device_id, locations, device_status    │
└────────────────┬────────────────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────────────────┐
│ 6. SEND TO BUOY API                                         │
│    - POST /api/v1.0/gear/ for each payload                  │
│    - Track success/failure counts                           │
│    - Log activity to Gundi                                  │
│    - Return results                                         │
└─────────────────────────────────────────────────────────────┘
```

---

## Configuration

### Sync Window

**Default**: 30 minutes
**Override**: 90 days (configured in handler)

```python
# In action_pull_edgetech_observations:
action_config.minutes_to_sync = 90 * 24 * 60  # 129,600 minutes
```

**Reasoning**: Ensures full historical sync on each run to catch any missed updates

### Polling Limits

```python
num_get_retry: 60  # Maximum polling attempts
retry_interval: 1 second
```

**Total max wait**: 60 seconds for dump generation

### Execution Schedule

```python
@crontab_schedule("*/3 * * * *")  # Every 3 minutes
```

**Frequency**: 480 executions per day

---

## Error Handling and Logging

### Critical Errors

**InvalidCredentials**
```python
# Raised when OAuth token refresh fails
return {"valid_credentials": False, "error": str(e)}
```

**Missing End Unit**
```python
logger.warning(
    f"End unit {endUnit} not found for {serialNumber}, skipping deployment."
)
continue  # Skip entire system, don't fail
```

### Information Logging

**Filtered Buoys**
```python
# Buoys without location that aren't in hauled/recovered state
logger.warning(f"Skipping buoy {serialNumber} with no location data and not hauled/recovered.")
# Hauled buoys are logged at INFO level (not skipped)
logger.info(f"Processing hauled/recovered buoy {serialNumber} without recovery location...")
```

**Operation Counts**
```python
logger.info(f"Buoys to deploy: {to_deploy}")
logger.info(f"Buoys to haul: {to_haul}")
logger.info(f"Buoys to update: {to_update}")
```

**Gear Payloads Summary**
```python
logger.info(
    f"Generated {len(gear_payloads)} gear payload(s):\n"
    f"{json.dumps(gear_payloads, indent=4, default=str)}"
)
```

### Activity Logging

Each execution logs to Gundi:
```python
await log_action_activity(
    integration_id=integration.id,
    action_id="pull_edgetech",
    level=LogLevel.INFO,
    title="Pulled data from EdgeTech API"
)
```

---

## API Reference

### EdgeTech API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/oauth/token` | POST | Refresh authentication token |
| `/v1/database-dump/tasks` | POST | Initiate database dump |
| `/v1/database-dump/tasks/{id}` | GET | Check dump status |
| Download URL | GET | Retrieve GZIP file |

### Earth Ranger API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1.0/gear/` | GET | List existing buoy gears (filtered by manufacturer=edgetech) |
| `/api/v1.0/gear/` | POST | Create or update gear sets |
| `/api/v1.0/sources/` | GET | List existing sources (for device_id mapping) |

### Response Codes

| Code | Meaning | Action |
|------|---------|--------|
| 200 | OK | Continue processing |
| 303 | See Other | Follow Location header |
| 401 | Unauthorized | Refresh token |
| 404 | Not Found | Resource doesn't exist |

---

## Conclusion

This integration provides robust synchronization between EdgeTech's Trap Tracker system and EarthRanger via the Buoy API:

✅ **Automated OAuth management** with token refresh
✅ **Efficient database dump** mechanism for bulk data retrieval
✅ **Intelligent filtering** to process active and explicitly hauled buoys
✅ **Explicit status-based haul detection** (not inferred from absence)
✅ **Re-deployment handling**: when EdgeTech `dateDeployed` is meaningfully later than ER’s deployment, we close the previous gear and create a new one (hauls sent before new deployments). Haul timestamps and recovery locations are sourced from `changeRecords` when a rapid haul+redeploy clears `currentState`. When EdgeTech skips the haul stage entirely (no `dateRecovered` anywhere), the synthetic haul uses `dateDeployed - 1s` as a deterministic `recorded_at` to avoid collisions with the deploy payload.
✅ **Recovery from missed deployments**: if a re-deployment haul succeeded but the deploy failed, subsequent runs detect the hauled ER gear vs deployed EdgeTech state and create the missing deployment
✅ **Set ID resolution** to correctly update existing vs create new gear sets
✅ **Support for complex systems** including two-unit lines
✅ **Standardized gear payload format** for the Buoy API
✅ **Comprehensive logging** for monitoring and debugging

The system runs every 3 minutes, maintaining near real-time synchronization while respecting API limits and ensuring data quality.
