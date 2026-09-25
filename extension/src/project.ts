import { URLExt } from '@jupyterlab/coreutils';
import { ServerConnection } from '@jupyterlab/services';
import { Signal } from '@lumino/signaling';

import type { IProjectContext, Project, ProjectState } from './tokens';

export class ProjectContext implements IProjectContext {
  private _path = '';
  private _request = 0;
  private _state: ProjectState = { status: 'empty', project: null };
  private _settings = ServerConnection.makeSettings();
  readonly changed: Signal<IProjectContext, void> = new Signal(this);

  get state(): ProjectState {
    return this._state;
  }

  async setPath(path: string): Promise<void> {
    this._path = path;
    await this.refresh();
  }

  async refresh(): Promise<void> {
    const request = ++this._request;
    this._setState({ status: 'loading', project: null });
    try {
      const url = URLExt.join(this._settings.baseUrl, 'solo', 'project');
      const response = await ServerConnection.makeRequest(
        `${url}?${new URLSearchParams({ path: this._path })}`,
        {},
        this._settings
      );
      if (!response.ok) {
        throw await ServerConnection.ResponseError.create(response);
      }
      const { project } = (await response.json()) as { project: Project | null };
      if (request === this._request) {
        this._setState({
          status: project ? 'ready' : 'empty',
          project: project ? Object.freeze(project) : null
        });
      }
    } catch (error) {
      if (request === this._request) {
        this._setState({
          status: 'error',
          project: null,
          error: error instanceof Error ? error.message : String(error)
        });
      }
    }
  }

  private _setState(state: ProjectState): void {
    this._state = Object.freeze(state);
    this.changed.emit();
  }
}
