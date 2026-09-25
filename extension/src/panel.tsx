import { ReactWidget } from '@jupyterlab/apputils';
import React from 'react';

import type { IProjectContext, ProjectKind } from './tokens';
import { ProjectVersions } from './versions';

export const kinds: Record<ProjectKind, string> = {
  factor: '因子分析',
  model: '策略建模',
  optimize: '组合优化',
  control: '订单风控',
  execution: '算法下单'
};

/** 项目元数据以文本展示，字体和颜色继承 Jupyter 主题。 */
export class ProjectDetails extends ReactWidget {
  constructor(private readonly context: IProjectContext, private readonly saveFiles: () => Promise<void>) {
    super();
    this.title.label = '当前项目';
    this.addClass('solo-project-details');
    context.changed.connect(this.onChanged, this);
  }

  dispose(): void {
    this.context.changed.disconnect(this.onChanged, this);
    super.dispose();
  }

  private onChanged(): void {
    this.update();
  }

  render(): React.ReactElement {
    const { project, status, error } = this.context.state;
    if (!project) {
      const message = status === 'loading'
        ? '正在读取项目…'
        : status === 'error'
          ? `读取失败：${error}`
          : '打开研究项目中的文件，或在文件浏览器进入项目目录。';
      return <p className="solo-project-message" role="status">{message}</p>;
    }
    return <><dl className="solo-project-metadata">
      <dt>项目</dt><dd>{project.name}</dd>
      <dt>类型</dt><dd>{kinds[project.kind]}</dd>
      <dt>Scheme</dt><dd>{project.scheme_version ?? '未记录'}</dd>
      <dt>Algo</dt><dd>{project.algo_version ?? '未记录'}</dd>
      <dt>目录</dt><dd>{project.path || '/'}</dd>
    </dl><ProjectVersions key={project.project_id} project={project} saveFiles={this.saveFiles} /></>;
  }
}
