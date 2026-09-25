import { Token } from '@lumino/coreutils';
import type { ISignal } from '@lumino/signaling';

export type ProjectKind = 'factor' | 'model' | 'optimize' | 'control' | 'execution';

export interface Project {
  readonly path: string;
  readonly project_id: string;
  readonly name: string;
  readonly kind: ProjectKind;
  readonly scheme_version: string | null;
  readonly scheme_commit: string | null;
  readonly algo_version: string | null;
  readonly algo_commit: string | null;
}

export interface ProjectState {
  readonly status: 'loading' | 'ready' | 'empty' | 'error';
  readonly project: Project | null;
  readonly error?: string;
}

/** 后续提交、上游管理等插件通过此服务获取当前项目。 */
export interface IProjectContext {
  readonly state: ProjectState;
  readonly changed: ISignal<IProjectContext, void>;
  refresh(): Promise<void>;
}

export const IProjectContext = new Token<IProjectContext>(
  '@solo/jupyter-extension:IProjectContext'
);
