/**
 * Maps Mimir catalog API responses to Backstage Entity objects.
 *
 * Each Mimir repo becomes a Backstage Component entity.
 * Each API endpoint becomes a Backstage API entity.
 * Cross-repo dependencies become dependsOn / consumesApis relations.
 */

import type { Entity } from '@backstage/catalog-model';
import type { MimirCatalogResponse, MimirServiceEntry, MimirCatalogApi } from './types';

/** Sanitize a string to a valid Backstage entity name (lowercase kebab-case). */
function sanitizeName(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9-]/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '');
}

/** Build a unique API entity name from repo + endpoint path. */
function apiEntityName(repo: string, api: MimirCatalogApi): string {
  const pathPart = api.path
    .replace(/^\//, '')
    .replace(/[{}]/g, '')
    .replace(/\//g, '-');
  return sanitizeName(`${repo}-${api.method.toLowerCase()}-${pathPart}`);
}

/** Create a Backstage Component entity from a Mimir service entry. */
function mapServiceToComponent(service: MimirServiceEntry): Entity {
  const name = sanitizeName(service.repo);

  // Build tags from languages and frameworks
  const tags: string[] = [
    ...Object.keys(service.tech_stack.languages),
    ...service.tech_stack.frameworks.map(f => sanitizeName(f)),
  ];

  // Build providesApis refs
  const providesApis = service.apis.map(api =>
    `api:default/${apiEntityName(service.repo, api)}`,
  );

  // Build consumesApis refs (from api_calls dependencies)
  const consumesApis = service.dependencies
    .filter(d => d.dependency_type === 'api_calls')
    .map(d => {
      // We reference the target repo's component — the specific API
      // isn't known from the dependency edge alone
      return `component:default/${sanitizeName(d.target_repo)}`;
    });

  // Build dependsOn refs (from shared_lib / imports dependencies)
  const dependsOn = service.dependencies
    .filter(d => d.dependency_type !== 'api_calls')
    .map(d => `component:default/${sanitizeName(d.target_repo)}`);

  // Determine component type
  const hasApis = service.apis.length > 0;
  const type = hasApis ? 'service' : 'library';

  return {
    apiVersion: 'backstage.io/v1alpha1',
    kind: 'Component',
    metadata: {
      name,
      description: `Auto-discovered by Mimir code graph analysis`,
      annotations: {
        'mimir.dev/repo': service.repo,
        'mimir.dev/quality-score': String(service.quality_score),
        'mimir.dev/node-id': service.node_id,
      },
      tags: [...new Set(tags)],
    },
    spec: {
      type,
      lifecycle: 'production',
      owner: service.owner ?? 'unknown',
      ...(providesApis.length > 0 && { providesApis }),
      ...(consumesApis.length > 0 && { consumesApis }),
      ...(dependsOn.length > 0 && { dependsOn }),
    },
  };
}

/** Create a Backstage API entity from a Mimir API endpoint. */
function mapApiToEntity(repo: string, api: MimirCatalogApi, owner: string): Entity {
  const name = apiEntityName(repo, api);

  return {
    apiVersion: 'backstage.io/v1alpha1',
    kind: 'API',
    metadata: {
      name,
      description: `${api.method} ${api.path} — ${api.containing_function}`,
      annotations: {
        'mimir.dev/node-id': api.node_id,
        'mimir.dev/method': api.method,
        'mimir.dev/path': api.path,
        'mimir.dev/repo': repo,
      },
    },
    spec: {
      type: 'openapi',
      lifecycle: 'production',
      owner,
      definition: [
        `# Auto-discovered by Mimir`,
        `# Method: ${api.method}`,
        `# Path: ${api.path}`,
        `# Function: ${api.containing_function}`,
      ].join('\n'),
    },
  };
}

/**
 * Transform a full Mimir catalog response into Backstage entities.
 *
 * Returns a flat array of Component and API entities.
 */
export function mapMimirToEntities(response: MimirCatalogResponse): Entity[] {
  const entities: Entity[] = [];

  for (const service of response.services) {
    // Component entity for the service/repo
    entities.push(mapServiceToComponent(service));

    // API entities for each endpoint
    const owner = service.owner ?? 'unknown';
    for (const api of service.apis) {
      entities.push(mapApiToEntity(service.repo, api, owner));
    }
  }

  return entities;
}
