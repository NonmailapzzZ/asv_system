## [16 May 2026] Selective Auto-Sampling during Missions
Modified: pixhawk_reader.py (138-193: Added mission status tracking and updated read_all to include MISSION_ITEM_REACHED)
Modified: app.py (295-400: Refactored pixhawk_thread to selectively trigger sampling only on Delay/Loiter commands and reset state on mission sequence change)
Added: app.py (76: Added mission_item to global state)
Deleted: pixhawk_reader.py (read_mission_current: Replaced with more comprehensive read_mission_status)
Impact: Sampling now only occurs du ring intended Delay (93), Loiter Time (19), or custom triggers (184), and specifically in LOITER mode. It no longer triggers at every waypoint, preventing data clutter during mission navigation.
