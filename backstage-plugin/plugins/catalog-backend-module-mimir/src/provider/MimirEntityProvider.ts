/**
 * Backstage EntityProvider that auto-populates the catalog from Mimir's
 * code graph API. Periodically fetches /api/v1/catalog and applies
 * full mutations to the Backstage catalog.
 */

import type {
  EntityProvider,
  EntityProviderConnection,
} from '@backstage/plugin-catalog-node';
import type { LoggerService, SchedulerService, SchedulerServiceTaskRunner } from '@backstage/backend-plugin-api';
import type { MimirCatalogResponse } from './types';
import type { MimirProviderConfig } from '../config';
import { mapMimirToEntities } from './mapper';

const PROVIDER_NAME = 'mimir';

export class MimirEntityProvider implements EntityProvider {
  private connection?: EntityProviderConnection;
  private readonly baseUrl: string;
  private readonly repoFilters?: string[];
  private readonly logger: LoggerService;
  private readonly taskRunner: SchedulerServiceTaskRunner;

  constructor(options: {
    config: MimirProviderConfig;
    logger: LoggerService;
    taskRunner: SchedulerServiceTaskRunner;
  }) {
    this.baseUrl = options.config.baseUrl.replace(/\/$/, '');
    this.repoFilters = options.config.repoFilters;
    this.logger = options.logger;
    this.taskRunner = options.taskRunner;
  }

  getProviderName(): string {
    return PROVIDER_NAME;
  }

  async connect(connection: EntityProviderConnection): Promise<void> {
    this.connection = connection;

    await this.taskRunner.run({
      id: `${PROVIDER_NAME}-entity-provider-refresh`,
      fn: async () => {
        await this.refresh();
      },
    });
  }

  private async refresh(): Promise<void> {
    if (!this.connection) {
      throw new Error('MimirEntityProvider not connected');
    }

    const reposParam = this.repoFilters?.join(',') ?? '';
    const url = `${this.baseUrl}/api/v1/catalog${reposParam ? `?repos=${encodeURIComponent(reposParam)}` : ''}`;

    this.logger.info(`Fetching catalog from Mimir: ${url}`);

    let data: MimirCatalogResponse;
    try {
      const response = await fetch(url);
      if (!response.ok) {
        throw new Error(`Mimir API returned ${response.status}: ${response.statusText}`);
      }
      data = (await response.json()) as MimirCatalogResponse;
    } catch (error) {
      this.logger.error(`Failed to fetch from Mimir: ${error}`);
      return;
    }

    const entities = mapMimirToEntities(data);
    this.logger.info(
      `Mimir discovered ${data.services.length} services, ${entities.length} total entities`,
    );

    await this.connection.applyMutation({
      type: 'full',
      entities: entities.map(entity => ({
        entity,
        locationKey: `${PROVIDER_NAME}-provider`,
      })),
    });
  }
}
