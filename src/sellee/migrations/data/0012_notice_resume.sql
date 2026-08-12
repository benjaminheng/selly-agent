-- A chunked notice's delivery progress: how many of its chunks already reached the seller. Long
-- notice text is chunked and sent as a sequential loop of provider API calls, so a failure partway
-- leaves earlier chunks delivered; without a persisted resume point every retry restarts at chunk
-- 0 and duplicates them. drain_notices advances it; the provider's send resumes from it.
ALTER TABLE notices ADD COLUMN sent_chunks INTEGER NOT NULL DEFAULT 0;
