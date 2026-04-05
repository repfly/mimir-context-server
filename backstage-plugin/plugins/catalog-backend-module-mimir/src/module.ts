/**
 * Backstage backend module that registers the Mimir entity provider
 * with the catalog plugin.
 *
 * Usage in a Backstage app's backend:
 *   import { catalogModuleMimirProvider } from '@mimir/plugin-catalog-backend-module-mimir';
 *   backend.add(catalogModuleMimirProvider);
 *
 * Configuration in app-config.yaml:
 *   catalog:
 *     providers:
 *       mimir:
 *         baseUrl: http://localhost:8421
 *         refreshIntervalMinutes: 30
 *         repoFilters:
 *           - my-service
 */

import {
  coreServices,
  createBackendModule,
} from '@backstage/backend-plugin-api';
import { catalogProcessingExtensionPoint } from '@backstage/plugin-catalog-node/alpha';
import { MimirEntityProvider } from './provider/MimirEntityProvider';
import type { MimirProviderConfig } from './config';

export const catalogModuleMimirProvider = createBackendModule({
  pluginId: 'catalog',
  moduleId: 'mimir-provider',
  register(reg) {
    reg.registerInit({
      deps: {
        catalog: catalogProcessingExtensionPoint,
        logger: coreServices.logger,
        scheduler: coreServices.scheduler,
        config: coreServices.rootConfig,
      },
      async init({ catalog, logger, scheduler, config }) {
        const mimirConfig = config.getConfig('catalog.providers.mimir');

        const providerConfig: MimirProviderConfig = {
          baseUrl: mimirConfig.getString('baseUrl'),
          refreshIntervalMinutes: mimirConfig.getOptionalNumber('refreshIntervalMinutes') ?? 30,
          repoFilters: mimirConfig.getOptionalStringArray('repoFilters'),
        };

        const taskRunner = scheduler.createScheduledTaskRunner({
          frequency: { minutes: providerConfig.refreshIntervalMinutes ?? 30 },
          timeout: { minutes: 5 },
        });

        const provider = new MimirEntityProvider({
          config: providerConfig,
          logger,
          taskRunner,
        });

        catalog.addEntityProvider(provider);

        logger.info(
          `Mimir catalog provider registered (baseUrl: ${providerConfig.baseUrl}, ` +
          `refresh: ${providerConfig.refreshIntervalMinutes}m)`,
        );
      },
    });
  },
});
