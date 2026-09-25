import { Button, Option, Select } from '@jupyter/react-components';
import { ReactWidget } from '@jupyterlab/apputils';
import { URLExt } from '@jupyterlab/coreutils';
import { ServerConnection } from '@jupyterlab/services';
import React, { useEffect, useState } from 'react';

import { kinds } from './panel';
import type { IProjectContext, Project, ProjectKind } from './tokens';

interface Candidate {
  id: string;
  name: string;
  kind: ProjectKind;
  schemeVersion: string;
}

async function request<T>(path: string, projectId?: string): Promise<T> {
  const settings = ServerConnection.makeSettings();
  const url = URLExt.join(settings.baseUrl, 'solo', 'project', 'dependencies');
  const response = await ServerConnection.makeRequest(
    projectId ? url : `${url}?${new URLSearchParams({ path })}`,
    projectId ? {
      method: 'POST',
      body: JSON.stringify({ path, project_id: projectId }),
      headers: { 'Content-Type': 'application/json' }
    } : {},
    settings
  );
  if (!response.ok) {
    const body = await response.clone().json().catch(() => null) as { reason?: string } | null;
    if (body?.reason) {
      throw new Error(body.reason);
    }
    throw await ServerConnection.ResponseError.create(response);
  }
  return response.json() as Promise<T>;
}

function Installer({ project }: { project: Project }): React.ReactElement {
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [selected, setSelected] = useState('');
  const [loading, setLoading] = useState(true);
  const [installing, setInstalling] = useState(false);
  const [message, setMessage] = useState('');
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setMessage('');
    request<{ projects: Candidate[] }>(project.path).then(({ projects }) => {
      if (active) {
        setCandidates(projects);
        setSelected(projects[0]?.id ?? '');
      }
    }).catch((error: Error) => {
      if (active) {
        setCandidates([]);
        setSelected('');
        setMessage(error.message);
      }
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [project.path, refresh]);

  async function install(): Promise<void> {
    setInstalling(true);
    setMessage('正在使用 uv 安装…');
    try {
      const result = await request<{ package: string }>(project.path, selected);
      setMessage(`已安装 ${result.package}。已加载该包的 Kernel 需重启后使用新代码。`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setInstalling(false);
    }
  }

  return <>
    <Select key={loading ? 'loading' : candidates.length ? 'projects' : 'empty'}
      aria-label="已有项目" value={selected} disabled={loading || installing || !candidates.length}
      onChange={(event) => setSelected((event.target as HTMLSelectElement).value)}>
      {candidates.length ? candidates.map((candidate) =>
        <Option key={candidate.id} value={candidate.id}>
          {kinds[candidate.kind]} / {candidate.name} · Scheme {candidate.schemeVersion}
        </Option>
      ) : <Option value="">{loading ? '正在读取项目…' : '没有兼容的同类或上游项目'}</Option>}
    </Select>
    <div className="solo-project-actions">
      <Button appearance="accent" disabled={loading || installing || !selected} onClick={() => void install()}>
        {installing ? '安装中…' : '安装项目'}
      </Button>
      <Button disabled={loading || installing} onClick={() => setRefresh((value) => value + 1)}>刷新列表</Button>
    </div>
    {message && <p className="solo-project-message" role="status" aria-label="安装结果">{message}</p>}
  </>;
}

export class ProjectDependencies extends ReactWidget {
  constructor(private readonly context: IProjectContext) {
    super();
    this.title.label = '安装已有项目';
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
    const { project } = this.context.state;
    return project
      ? <Installer key={`${project.project_id}:${project.path}`} project={project} />
      : <p className="solo-project-message" role="status">请先打开研究项目。</p>;
  }
}
