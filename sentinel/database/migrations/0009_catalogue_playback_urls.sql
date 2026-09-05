-- Playback URLs as stored facts, not as string surgery at request time.
--
-- The API used to build these from a base URL:
--
--     whep  = f"{base}/cam-{id}/whep"
--     hls   = base.replace('8889', '8888') + f"/cam-{id}/index.m3u8"
--
-- Two defects in three lines. The `cam-<id>` prefix is a local MediaMTX
-- convention that no upstream gateway shares, and the port substitution
-- silently produces a wrong URL whenever the base port is anything other
-- than 8889 -- including the case where 8889 appears inside a hostname.
-- Neither fails loudly; both yield a URL that resolves to nothing.
--
-- The Sentinel catalogue publishes these URLs per camera. They are
-- therefore stored per camera and served verbatim. The documented contract
-- shape is used only to DERIVE a URL the catalogue omitted, never to
-- rewrite one it supplied.
ALTER TABLE camera ADD COLUMN IF NOT EXISTS whep_url TEXT;
ALTER TABLE camera ADD COLUMN IF NOT EXISTS hls_url  TEXT;

COMMENT ON COLUMN camera.whep_url IS
    'Browser WebRTC playback endpoint, as published by the source catalogue.';
COMMENT ON COLUMN camera.hls_url IS
    'HLS fallback playlist, as published by the source catalogue.';
