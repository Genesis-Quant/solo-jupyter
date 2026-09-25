import {
  ILayoutRestorer,
  ILabShell,
  type JupyterFrontEnd,
  type JupyterFrontEndPlugin
} from '@jupyterlab/application';
import { ICommandPalette, showErrorMessage } from '@jupyterlab/apputils';
import { PathExt } from '@jupyterlab/coreutils';
import { IDocumentManager } from '@jupyterlab/docmanager';
import { IDefaultFileBrowser } from '@jupyterlab/filebrowser';
import {
  buildIcon,
  CommandToolbarButton,
  folderIcon,
  notebookIcon,
  refreshIcon,
  SidePanel
} from '@jupyterlab/ui-components';

import { ProjectDetails } from './panel';
import { ProjectDependencies } from './dependencies';
import { ProjectContext } from './project';
import { IProjectContext } from './tokens';
import '../style/index.css';

export { IProjectContext } from './tokens';
export type { Project, ProjectKind, ProjectState } from './tokens';

export const CommandIDs = {
  open: 'solo:open',
  refresh: 'solo:refresh-project',
  directory: 'solo:open-project-directory',
  notebook: 'solo:open-research-notebook',
  dependencies: 'solo:open-pyproject'
} as const;

const plugin: JupyterFrontEndPlugin<IProjectContext> = {
  id: '@solo/jupyter-extension:project',
  description: 'Solo 研究项目上下文与侧栏',
  autoStart: true,
  provides: IProjectContext,
  requires: [ILabShell, IDefaultFileBrowser, IDocumentManager, ICommandPalette, ILayoutRestorer],
  activate: (
    app: JupyterFrontEnd,
    shell: ILabShell,
    browser: IDefaultFileBrowser,
    documents: IDocumentManager,
    palette: ICommandPalette,
    restorer: ILayoutRestorer
  ): IProjectContext => {
    const context = new ProjectContext();
    const panel = new SidePanel();
    panel.id = 'solo-project-panel';
    panel.title.icon = buildIcon;
    panel.title.caption = 'Solo';
    panel.addWidget(new ProjectDetails(context, async () => {
      const project = context.state.project;
      if (!project) return;
      for (const widget of shell.widgets('main')) {
        const document = documents.contextForWidget(widget);
        if (document?.path.startsWith(project.path + '/') && document.model.dirty) {
          await document.save();
        }
      }
    }));
    panel.addWidget(new ProjectDependencies(context));
    const hasProject = (): boolean => context.state.status === 'ready';

    async function openFile(name: string): Promise<void> {
      const project = context.state.project;
      if (!project) {
        return;
      }
      try {
        const path = PathExt.join(project.path, name);
        await app.serviceManager.contents.get(path, { content: false });
        const widget = documents.openOrReveal(path);
        if (widget) {
          shell.activateById(widget.id);
        }
      } catch (error) {
        await showErrorMessage('无法打开项目文件', error instanceof Error ? error : String(error));
      }
    }

    app.commands.addCommand(CommandIDs.open, {
      label: '打开 Solo',
      execute: () => shell.activateById(panel.id)
    });
    app.commands.addCommand(CommandIDs.refresh, {
      label: '刷新项目',
      icon: refreshIcon,
      isEnabled: () => context.state.status !== 'loading',
      execute: () => context.refresh()
    });
    app.commands.addCommand(CommandIDs.directory, {
      label: '打开项目目录',
      icon: folderIcon,
      isEnabled: hasProject,
      execute: async () => {
        const project = context.state.project;
        if (!project) {
          return;
        }
        try {
          await browser.model.cd(`/${project.path}`);
          shell.activateById(browser.id);
        } catch (error) {
          await showErrorMessage('无法打开项目目录', error instanceof Error ? error : String(error));
        }
      }
    });
    app.commands.addCommand(CommandIDs.notebook, {
      label: '打开研究 Notebook',
      icon: notebookIcon,
      isEnabled: hasProject,
      execute: () => openFile('research.ipynb')
    });
    app.commands.addCommand(CommandIDs.dependencies, {
      label: '打开项目依赖',
      isEnabled: hasProject,
      execute: () => openFile('pyproject.toml')
    });
    for (const command of Object.values(CommandIDs)) {
      palette.addItem({ command, category: 'Solo' });
    }
    for (const command of [CommandIDs.refresh, CommandIDs.directory, CommandIDs.notebook]) {
      panel.toolbar.addItem(command, new CommandToolbarButton({
        commands: app.commands,
        id: command,
        label: ''
      }));
    }
    context.changed.connect(() => {
      for (const command of Object.values(CommandIDs)) {
        app.commands.notifyCommandChanged(command);
      }
    });
    shell.add(panel, 'left', { rank: 250 });
    restorer.add(panel, panel.id);

    // 文件切换/改名与目录导航都更新上下文；异步请求只应用最后一次结果。
    shell.currentPathChanged.connect((_, args) => {
      void context.setPath(args.newValue ?? browser.model.path);
    });
    browser.model.pathChanged.connect(() => {
      void context.setPath(browser.model.path);
    });
    app.serviceManager.contents.fileChanged.connect((_, change) => {
      const path = change.newValue?.path ?? change.oldValue?.path;
      if (path && PathExt.basename(path) === '.solo') {
        void context.refresh();
      }
    });
    void app.restored.then(() => context.setPath(shell.currentPath ?? browser.model.path));
    return context;
  }
};

export default plugin;
