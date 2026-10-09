import type { JupyterFrontEnd } from '@jupyterlab/application';
import type { ISessionContext } from '@jupyterlab/apputils';
import { URLExt } from '@jupyterlab/coreutils';
import { ServerConnection } from '@jupyterlab/services';

import type { Project } from './tokens';

const settings = ServerConnection.makeSettings();

async function projectKernel(path: string): Promise<string | null> {
  const url = URLExt.join(settings.baseUrl, 'solo', 'project');
  const response = await ServerConnection.makeRequest(
    `${url}?${new URLSearchParams({ path })}`,
    {},
    settings
  );
  if (!response.ok) {
    return null;
  }
  const { project } = (await response.json()) as { project: Project | null };
  return project ? `solo-${project.project_id}` : null;
}

/** 项目 Notebook 只能运行在该项目注册的 Kernel 中。 */
export async function bindProjectKernel(
  app: JupyterFrontEnd,
  session: ISessionContext,
  path: string
): Promise<void> {
  const name = await projectKernel(path);
  if (!name) {
    return;
  }
  await session.ready;
  if (session.isDisposed || session.session?.kernel?.name === name) {
    return;
  }
  // 新建项目的 Kernel 可能晚于前端缓存的 kernelspec 列表注册。
  const specs = app.serviceManager.kernelspecs;
  await specs.refreshSpecs();
  if (session.isDisposed || !specs.specs?.kernelspecs[name]) {
    return;
  }
  if (session.session?.kernel?.name !== name) {
    await session.changeKernel({ name });
  }
}
