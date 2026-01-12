# EdgeTech to Earth Ranger Set Conversion

## Overview

This document explains how EdgeTech buoy data is converted to Earth Ranger gear sets, focusing on how `set_id` is generated and managed.

## Key Concepts

### EdgeTech Data Model
- **Buoy**: Individual physical tracking device with a `serialNumber`
- **User**: Owner identified by `userId`
- **Two-Unit Lines**: Optional paired buoys (start + end)

### Earth Ranger Data Model
- **Subject**: Represents a gear set (the entire trap line deployment)
- **Source**: Represents individual tracking devices/buoys
- **SubjectSource**: Links devices to gear sets with temporal assignments

## Set ID Generation Strategy

In the EdgeTech integration, **`set_id` is generated dynamically** based on the buoy configuration:

### 1. Set ID from EdgeTech Buoys

**Location in code**: `gundi-integration-edgetech/app/actions/edgetech/processor.py`

```python
payload = {
    "set_id": set_id or str(uuid4()),  # Line 154
    "owner_id": buoy.userId,
    "manufacturer_name": "EdgeTech",
    ...
}
```

**Generation Logic**:
- If `set_id` is explicitly provided → use it
- Otherwise → **Generate a new UUID** using `uuid4()`

**Important**: EdgeTech buoys do NOT have a native "set_id" concept in their database. Each buoy is tracked individually by serial number and user ID. The integration **creates** the set_id concept to group buoys into gear sets.

### 2. Device ID Generation (mfr_device_id)

Device IDs are created from EdgeTech data using this pattern:

#### For Single-Unit Buoys (with start and end coordinates)

```python
# If buoy has endLatDeg and endLonDeg
main_device_id = f"{serialNumber}_{hashed_user_id}_A"      # Start point
secondary_device_id = f"{serialNumber}_{hashed_user_id}_B" # End point
```

**Example**:
```
EdgeTech Serial: 88CE99D359
User ID: 652e7174c0884e7f02ec97d1
Hashed User ID: a1b2c3d4 (anonymized)

Device A: edgetech_88CE99D359_a1b2c3d4_A
Device B: edgetech_88CE99D359_a1b2c3d4_B
```

Both devices belong to the **same `set_id`** (UUID).

#### For Two-Unit Line Buoys

```python
# Two physical buoys
start_device_id = f"{start_serial}_{hashed_user_id}"
end_device_id = f"{end_serial}_{hashed_user_id}"
```

**Example**:
```
Start Buoy Serial: 88CE99CE8A
End Buoy Serial: 88CE99CE8B
User ID: 684b1ec1c1df05abfa78b756
Hashed User ID: x9y8z7w6

Start Device: edgetech_88CE99CE8A_x9y8z7w6
End Device: edgetech_88CE99CE8B_x9y8z7w6
```

Both devices belong to the **same `set_id`** (UUID).

### 3. User ID Hashing

User IDs are hashed to anonymize them:

```python
def get_hashed_user_id(user_id: str) -> str:
    # Convert to hex
    user_id_hex = user_id.encode("utf-8").hex()
    
    # Hash with Hashids (min length: 8)
    hashids = Hashids(min_length=8)
    hashed = hashids.encode_hex(user_id_hex)
    
    return hashed
```

**Purpose**: Protect fisher privacy while maintaining uniqueness for device identification.

## Gear Payload Structure

When sending data to Earth Ranger, the EdgeTech processor creates this payload:

```json
{
  "set_id": "550e8400-e29b-41d4-a716-446655440000",  // Generated UUID or provided
  "owner_id": "652e7174c0884e7f02ec97d1",             // Original EdgeTech userId
  "manufacturer_name": "EdgeTech",
  "deployment_type": "single" | "trawl",              // "trawl" if > 1 device
  "devices_in_set": 2,
  "initial_deployment_date": "2025-09-17T13:00:00Z", // Optional, for new sets
  "devices": [
    {
      "device_id": "123e4567-...",                   // UUID for Source.id
      "mfr_device_id": "edgetech_88CE99D359_a1b2c3d4_A",
      "device_status": "deployed" | "hauled",
      "last_deployed": "2025-09-17T13:00:00Z",
      "last_updated": "2025-09-17T13:33:29Z",
      "location": {
        "latitude": 41.5157518,
        "longitude": -71.0715739
      },
      "device_additional_data": { /* Full EdgeTech buoy data */ }
    }
  ]
}
```

## Earth Ranger Processing

When Earth Ranger receives this payload, it processes it according to the rules in `[buoy](https://github.com/PADAS/das/blob/develop/docs/architecture/buoy.md`:

### Set ID Resolution (Priority Order)

1. **Explicit `set_id`**: If provided in payload, use it directly
2. **Lookup by `mfr_set_id`**: Search for existing Subject where `Subject.name == mfr_set_id`
3. **Infer from devices**: Find active Subject with SubjectSource relationships for ALL device_ids
4. **Generate new UUID**: If not found, create new Subject with new UUID

**Note**: EdgeTech payloads always provide `set_id`, so ER uses it directly (case #1).

### Subject Creation

```python
Subject.objects.create(
    id=set_id,                                    # UUID from payload
    name=mfr_set_id or set_id,                   # EdgeTech doesn't provide mfr_set_id
    subject_subtype="ropeless_buoy_gearset",
    is_active=True,
    additional={
        "display_id": mfr_set_id or set_id,
        "manufacturer": "EdgeTech",
        "last_updated": "...",
        # ... other EdgeTech metadata
    }
)
```

### Source Creation

For each device in the payload:

```python
Source.objects.get_or_create(
    provider=default_provider,
    manufacturer_id=device["mfr_device_id"],  # e.g., edgetech_88CE99D359_a1b2c3d4_A
    defaults={
        "id": device["device_id"],              # UUID from payload
        "additional": {...}
    }
)
```

**Important**: Sources are identified by `(provider, manufacturer_id)` tuple, NOT by `device_id`.

### SubjectSource Linking

```python
SubjectSource.objects.get_or_create(
    subject=subject,        # The gear set
    source=source,          # The device
)

# For deployed devices
subject_source.assigned_range = [recorded_at, datetime.max)
subject_source.location = Point(lon, lat)

# For hauled devices
subject_source.assigned_range = [original_start, recorded_at)
subject_source.location = Point(recovery_lon, recovery_lat)
```

## Examples

### Example 1: Single-Unit Buoy Deployment

**EdgeTech Database Record**:
```json
{
  "serialNumber": "88CE99D359",
  "userId": "652e7174c0884e7f02ec97d1",
  "currentState": {
    "isDeployed": true,
    "latDeg": 41.5157518,
    "lonDeg": -71.0715739,
    "endLatDeg": 41.5158000,
    "endLonDeg": -71.0716000,
    "dateDeployed": "2025-09-17T13:00:00.000Z",
    "lastUpdated": "2025-09-17T13:33:29.849Z"
  }
}
```

**Generated Gear Payload**:
```json
{
  "set_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",  // NEW UUID
  "owner_id": "652e7174c0884e7f02ec97d1",
  "manufacturer_name": "EdgeTech",
  "deployment_type": "trawl",  // 2 devices = trawl
  "devices_in_set": 2,
  "initial_deployment_date": "2025-09-17T13:00:00Z",
  "devices": [
    {
      "device_id": "uuid-1",
      "mfr_device_id": "edgetech_88CE99D359_hashed123_A",
      "device_status": "deployed",
      "location": {"latitude": 41.5157518, "longitude": -71.0715739}
    },
    {
      "device_id": "uuid-2",
      "mfr_device_id": "edgetech_88CE99D359_hashed123_B",
      "device_status": "deployed",
      "location": {"latitude": 41.5158000, "longitude": -71.0716000}
    }
  ]
}
```

**Earth Ranger Result**:
- **1 Subject** (gear set) with `id = a1b2c3d4-e5f6-7890-abcd-ef1234567890`
- **2 Sources** (devices A and B)
- **2 SubjectSources** linking both devices to the subject
- **2 Observations** (one at each location)

### Example 2: Hauling a Deployed Buoy

**EdgeTech Database Record** (same buoy, now recovered):
```json
{
  "serialNumber": "88CE99D359",
  "userId": "652e7174c0884e7f02ec97d1",
  "currentState": {
    "isDeployed": false,
    "dateRecovered": "2025-09-17T16:00:00.000Z",
    "recoveredLatDeg": 41.5160000,
    "recoveredLonDeg": -71.0720000,
    "lastUpdated": "2025-09-17T16:05:00.000Z"
  }
}
```

**Generated Haul Payload**:
```json
{
  "set_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",  // SAME UUID as deployment
  "manufacturer_name": "EdgeTech",
  "deployment_type": "trawl",
  "devices": [
    {
      "device_id": "uuid-1",  // Same as deployed
      "mfr_device_id": "edgetech_88CE99D359_hashed123_A",
      "device_status": "hauled",
      "location": {"latitude": 41.5160000, "longitude": -71.0720000}
    },
    {
      "device_id": "uuid-2",
      "mfr_device_id": "edgetech_88CE99D359_hashed123_B",
      "device_status": "hauled",
      "location": {"latitude": 41.5160000, "longitude": -71.0720000}
    }
  ]
}
```

**Earth Ranger Updates**:
- **Subject**: `is_active = False` (all devices hauled)
- **SubjectSources**: `assigned_range` closed at haul time
- **2 New Observations**: Haul events at recovery location

## Key Differences: EdgeTech vs Earth Ranger

| Aspect | EdgeTech | Earth Ranger |
|--------|----------|--------------|
| **Primary Entity** | Individual buoy by serial number | Gear set (Subject) containing devices |
| **Set ID** | Not present - buoys tracked individually | Generated UUID to group related devices |
| **Device Grouping** | Implicit via `isTwoUnitLine` + `startUnit`/`endUnit` | Explicit via Subject → SubjectSource → Source |
| **Status** | `isDeployed` (boolean) | `assigned_range` (temporal bounds) |
| **Recovery** | `dateRecovered`, `recoveredLatDeg/Lon` | SubjectSource with closed `assigned_range` |
| **History** | `changeRecords` array | Observation records over time |

## Set ID Persistence

**Question**: How does the integration know to use the same `set_id` for updates?

**Answer**: The integration looks up existing Earth Ranger gears by `mfr_device_id`:

```python
# Build mapping of device IDs to existing gears
er_gears_devices_id_to_gear = {
    device.mfr_device_id: gear
    for gear in er_gears
    for device in gear.devices
}

# When processing an EdgeTech buoy:
existing_gear = er_gears_devices_id_to_gear.get(mfr_device_id)
if existing_gear:
    set_id = existing_gear.display_id  # Reuse existing set_id
else:
    set_id = str(uuid4())  # Generate new set_id
```

**Key Point**: The `mfr_device_id` (e.g., `edgetech_88CE99D359_hashed123_A`) is the **stable identifier** that links EdgeTech buoys to Earth Ranger gear sets across updates.

## Summary

1. **EdgeTech has NO native "set" concept** - buoys are tracked individually
2. **Integration generates `set_id`** as a UUID to group related devices
3. **Device IDs (`mfr_device_id`)** are the stable identifiers:
   - Format: `edgetech_{serialNumber}_{hashedUserId}[_A|_B]`
   - Used to find existing gears and maintain consistency
4. **Single buoy → 2 devices** (A and B for start/end of trap line)
5. **Two-unit line → 2 devices** (one per physical buoy)
6. **`set_id` persists** by looking up existing gears via `mfr_device_id`

The integration effectively creates the "set" abstraction on top of EdgeTech's individual buoy tracking to fit Earth Ranger's gear set model.
