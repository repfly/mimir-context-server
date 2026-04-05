/** TypeScript interfaces mirroring Mimir's Python catalog domain models. */

export interface MimirCatalogApi {
  node_id: string;
  path: string;
  method: string;
  containing_function: string;
  repo: string;
}

export interface MimirServiceDependency {
  source_repo: string;
  target_repo: string;
  dependency_type: string;
  evidence: Array<{ source_node: string; target_node: string }>;
}

export interface MimirTechStack {
  languages: Record<string, number>;
  frameworks: string[];
  key_dependencies: string[];
}

export interface MimirServiceEntry {
  repo: string;
  node_id: string;
  owner?: string;
  apis: MimirCatalogApi[];
  dependencies: MimirServiceDependency[];
  dependents: MimirServiceDependency[];
  tech_stack: MimirTechStack;
  quality_score: number;
  quality_distribution: Record<string, number>;
  node_counts: Record<string, number>;
}

export interface MimirCatalogResponse {
  services: MimirServiceEntry[];
  generated_at: string;
}
