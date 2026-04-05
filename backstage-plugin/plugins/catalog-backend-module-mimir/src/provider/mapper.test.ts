import { mapMimirToEntities } from './mapper';
import type { MimirCatalogResponse } from './types';

const mockResponse: MimirCatalogResponse = {
  services: [
    {
      repo: 'payments-service',
      node_id: 'payments-service:',
      owner: 'user:alice@example.com',
      apis: [
        {
          node_id: 'payments-service:routes.py::create_payment',
          path: '/api/payments',
          method: 'POST',
          containing_function: 'create_payment',
          repo: 'payments-service',
        },
        {
          node_id: 'payments-service:routes.py::get_payment',
          path: '/api/payments/{id}',
          method: 'GET',
          containing_function: 'get_payment',
          repo: 'payments-service',
        },
      ],
      dependencies: [
        {
          source_repo: 'payments-service',
          target_repo: 'users-service',
          dependency_type: 'api_calls',
          evidence: [{ source_node: 'a', target_node: 'b' }],
        },
        {
          source_repo: 'payments-service',
          target_repo: 'shared-lib',
          dependency_type: 'shared_lib',
          evidence: [{ source_node: 'c', target_node: 'd' }],
        },
      ],
      dependents: [],
      tech_stack: {
        languages: { python: 20, yaml: 2 },
        frameworks: ['Flask', 'SQLAlchemy'],
        key_dependencies: ['flask', 'sqlalchemy', 'celery'],
      },
      quality_score: 0.75,
      quality_distribution: { good: 15, moderate: 3, poor: 2 },
      node_counts: { file: 20, function: 40, api_endpoint: 2 },
    },
    {
      repo: 'shared-lib',
      node_id: 'shared-lib:',
      apis: [],
      dependencies: [],
      dependents: [
        {
          source_repo: 'payments-service',
          target_repo: 'shared-lib',
          dependency_type: 'shared_lib',
          evidence: [{ source_node: 'c', target_node: 'd' }],
        },
      ],
      tech_stack: {
        languages: { python: 10 },
        frameworks: [],
        key_dependencies: [],
      },
      quality_score: 0.6,
      quality_distribution: { good: 5, moderate: 3, poor: 2 },
      node_counts: { file: 10, function: 20 },
    },
  ],
  generated_at: '2026-04-03T12:00:00Z',
};

describe('mapMimirToEntities', () => {
  const entities = mapMimirToEntities(mockResponse);

  it('creates correct total number of entities', () => {
    // 2 Component + 2 API (only payments-service has APIs)
    expect(entities).toHaveLength(4);
  });

  it('creates Component entities for each repo', () => {
    const components = entities.filter(e => e.kind === 'Component');
    expect(components).toHaveLength(2);
    const names = components.map(c => c.metadata.name);
    expect(names).toContain('payments-service');
    expect(names).toContain('shared-lib');
  });

  it('creates API entities for endpoints', () => {
    const apis = entities.filter(e => e.kind === 'API');
    expect(apis).toHaveLength(2);
  });

  it('sets correct component type based on APIs', () => {
    const components = entities.filter(e => e.kind === 'Component');
    const payments = components.find(c => c.metadata.name === 'payments-service');
    const lib = components.find(c => c.metadata.name === 'shared-lib');
    expect(payments?.spec?.type).toBe('service');
    expect(lib?.spec?.type).toBe('library');
  });

  it('includes Mimir annotations', () => {
    const payments = entities.find(
      e => e.kind === 'Component' && e.metadata.name === 'payments-service',
    );
    expect(payments?.metadata.annotations?.['mimir.dev/repo']).toBe('payments-service');
    expect(payments?.metadata.annotations?.['mimir.dev/quality-score']).toBe('0.75');
  });

  it('includes language and framework tags', () => {
    const payments = entities.find(
      e => e.kind === 'Component' && e.metadata.name === 'payments-service',
    );
    const tags = payments?.metadata.tags ?? [];
    expect(tags).toContain('python');
    expect(tags).toContain('flask');
    expect(tags).toContain('sqlalchemy');
  });

  it('maps api_calls dependencies to consumesApis', () => {
    const payments = entities.find(
      e => e.kind === 'Component' && e.metadata.name === 'payments-service',
    );
    const consumesApis = (payments?.spec as any)?.consumesApis ?? [];
    expect(consumesApis).toContain('component:default/users-service');
  });

  it('maps shared_lib dependencies to dependsOn', () => {
    const payments = entities.find(
      e => e.kind === 'Component' && e.metadata.name === 'payments-service',
    );
    const dependsOn = (payments?.spec as any)?.dependsOn ?? [];
    expect(dependsOn).toContain('component:default/shared-lib');
  });

  it('maps providesApis for services with endpoints', () => {
    const payments = entities.find(
      e => e.kind === 'Component' && e.metadata.name === 'payments-service',
    );
    const providesApis = (payments?.spec as any)?.providesApis ?? [];
    expect(providesApis).toHaveLength(2);
  });

  it('does not include empty relation arrays', () => {
    const lib = entities.find(
      e => e.kind === 'Component' && e.metadata.name === 'shared-lib',
    );
    expect((lib?.spec as any)?.providesApis).toBeUndefined();
    expect((lib?.spec as any)?.consumesApis).toBeUndefined();
    expect((lib?.spec as any)?.dependsOn).toBeUndefined();
  });

  it('API entities have correct annotations', () => {
    const api = entities.find(e => e.kind === 'API');
    expect(api?.metadata.annotations?.['mimir.dev/method']).toBeDefined();
    expect(api?.metadata.annotations?.['mimir.dev/path']).toBeDefined();
    expect(api?.metadata.annotations?.['mimir.dev/repo']).toBe('payments-service');
  });

  it('handles empty response', () => {
    const result = mapMimirToEntities({ services: [], generated_at: '' });
    expect(result).toHaveLength(0);
  });

  it('uses owner from Mimir response for Component entities', () => {
    const payments = entities.find(
      e => e.kind === 'Component' && e.metadata.name === 'payments-service',
    );
    expect(payments?.spec?.owner).toBe('user:alice@example.com');
  });

  it('falls back to unknown when owner is missing', () => {
    const lib = entities.find(
      e => e.kind === 'Component' && e.metadata.name === 'shared-lib',
    );
    expect(lib?.spec?.owner).toBe('unknown');
  });

  it('propagates owner to API entities', () => {
    const api = entities.find(e => e.kind === 'API');
    expect(api?.spec?.owner).toBe('user:alice@example.com');
  });
});
