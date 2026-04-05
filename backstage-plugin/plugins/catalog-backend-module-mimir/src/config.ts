/** Configuration interface for the Mimir catalog provider. */

export interface MimirProviderConfig {
  /** Base URL of the Mimir HTTP server, e.g. "http://localhost:8421" */
  baseUrl: string;
  /** How often to refresh the catalog (minutes). Default: 30 */
  refreshIntervalMinutes?: number;
  /** Only sync specific repos. If omitted, all repos are synced. */
  repoFilters?: string[];
}
