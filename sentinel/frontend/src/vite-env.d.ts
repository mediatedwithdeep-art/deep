/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_URL?: string
}
interface ImportMeta {
  readonly env: ImportMetaEnv
}

/** Minimal GeoJSON shapes. Avoids a @types/geojson dependency for the
 *  handful of places the API hands us a FeatureCollection. */
declare namespace GeoJSON {
  interface Feature {
    type: 'Feature'
    geometry: { type: string; coordinates: any } | null
    properties: Record<string, any> | null
  }
  interface FeatureCollection {
    type: 'FeatureCollection'
    features: Feature[]
    [key: string]: any
  }
}
