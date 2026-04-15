CREATE TABLE IF NOT EXISTS person_face_exclusion (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL,
    face_observation_id INTEGER NOT NULL,
    assignment_id INTEGER,
    reason TEXT NOT NULL DEFAULT 'manual_exclude',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (person_id) REFERENCES person(id),
    FOREIGN KEY (face_observation_id) REFERENCES face_observation(id),
    FOREIGN KEY (assignment_id) REFERENCES person_face_assignment(id),
    UNIQUE (person_id, face_observation_id)
);

CREATE INDEX IF NOT EXISTS idx_person_face_exclusion_observation_active
    ON person_face_exclusion(face_observation_id, active);

CREATE INDEX IF NOT EXISTS idx_person_face_exclusion_person_active
    ON person_face_exclusion(person_id, active);
