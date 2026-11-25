ALTER TABLE parking_events ADD COLUMN sender_event_id text;
CREATE UNIQUE INDEX parking_events_sender_event_id
    ON parking_events (sender_event_id) WHERE sender_event_id IS NOT NULL;

-- Existing conflicts cause migration to roll back for operator review.
CREATE UNIQUE INDEX parking_sessions_active_slot
    ON parking_sessions (camera_id, slot_id)
    WHERE slot_id IS NOT NULL AND status = 'parked';
