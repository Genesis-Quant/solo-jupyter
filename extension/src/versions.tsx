import { Button, Checkbox, DateField, NumberField, Option, Select, TextField } from '@jupyter/react-components';
import { Dialog, ReactWidget } from '@jupyterlab/apputils';
import { URLExt } from '@jupyterlab/coreutils';
import { ServerConnection } from '@jupyterlab/services';
import React, { useEffect, useState } from 'react';
import type { Project } from './tokens';

interface Version { id: string; number: number; phase: string; error?: string }
interface ParameterField {
  key: string; label: string; control: 'date' | 'number' | 'select' | 'text' | 'checkbox';
  min?: number; max?: number; step?: number;
  options?: { value: string | number | boolean; label: string }[];
  itemType?: string;
  nullable?: boolean;
}
interface JsonSchema {
  $ref?: string; $defs?: Record<string, JsonSchema>;
  type?: string; format?: string; title?: string;
  properties?: Record<string, JsonSchema>; items?: JsonSchema;
  enum?: (string | number | boolean)[]; anyOf?: JsonSchema[];
  const?: string | number | boolean;
  minimum?: number; maximum?: number;
  'x-hidden'?: boolean; 'x-enum-labels'?: string[];
}
interface ParameterPanel {
  protocol: number;
  title: string;
  values: Record<string, unknown>;
  schemas: Record<string, JsonSchema>;
}

function parameterFields(schema: JsonSchema, prefix: string, root = schema): ParameterField[] {
  const resolve = (spec: JsonSchema): JsonSchema => spec.$ref
    ? { ...root.$defs?.[spec.$ref.split('/').pop()!], ...spec, $ref: undefined } : spec;
  return Object.entries(schema.properties ?? {}).flatMap(([name, raw]) => {
    let spec = resolve(raw);
    if (spec['x-hidden']) return [];
    const nullable = spec.anyOf?.some((item) => item.type === 'null');
    if (spec.anyOf) {
      const alternatives = spec.anyOf.filter((item) => item.type !== 'null');
      if (alternatives.length !== 1) throw new Error(`不支持多类型参数：${prefix}.${name}`);
      spec = { ...resolve(alternatives[0]), ...spec, anyOf: undefined };
    }
    const key = `${prefix}.${name}`;
    if (spec.type === 'object') return parameterFields(spec, key, root);
    const base = { key, label: spec.title ?? name, nullable };
    const choices = spec.enum ?? (spec.const !== undefined ? [spec.const] : undefined);
    if (choices) return [{ ...base, control: 'select' as const, options: choices.map((value, index) => ({ value, label: spec['x-enum-labels']?.[index] ?? String(value) })) }];
    if (spec.type === 'integer' || spec.type === 'number') return [{ ...base, control: 'number' as const, min: spec.minimum, max: spec.maximum, step: spec.type === 'integer' ? 1 : undefined }];
    if (spec.type === 'boolean') return [{ ...base, control: 'checkbox' as const }];
    if (spec.type === 'array') {
      const itemType = spec.items && resolve(spec.items).type;
      if (!itemType || !['string', 'number', 'integer'].includes(itemType)) throw new Error(`不支持的列表参数：${key}`);
      return [{ ...base, label: `${base.label}（逗号分隔）`, control: 'text' as const, itemType }];
    }
    if (spec.type === 'string') return [{ ...base, control: spec.format === 'date' ? 'date' as const : 'text' as const }];
    throw new Error(`不支持的参数类型：${key}`);
  });
}

async function request<T>(path: string, body?: object, schema = false): Promise<T> {
  const settings = ServerConnection.makeSettings();
  const url = URLExt.join(settings.baseUrl, 'solo/project/versions');
  const query = new URLSearchParams({ path, ...(schema ? { parameters: '1' } : {}) });
  const response = await ServerConnection.makeRequest(body ? url : `${url}?${query}`, body ? {
    method: 'POST', body: JSON.stringify({ path, ...body }), headers: { 'Content-Type': 'application/json' }
  } : {}, settings);
  if (!response.ok) {
    const data = await response.clone().json().catch(() => null) as { reason?: string } | null;
    if (data?.reason) throw new Error(data.reason);
    throw await ServerConnection.ResponseError.create(response);
  }
  return response.json() as Promise<T>;
}

class ParameterBody extends ReactWidget {
  values: Record<string, unknown>;
  readonly fields: ParameterField[];
  note = '';
  error = '';
  busy = false;
  constructor(readonly definition: ParameterPanel) {
    super();
    this.values = structuredClone(definition.values);
    this.fields = Object.entries(definition.schemas).flatMap(([key, schema]) => parameterFields(schema, key));
  }
  private value(key: string): unknown {
    return key.split('.').reduce<unknown>((value, part) => (value as Record<string, unknown> | undefined)?.[part], this.values);
  }
  private change(field: ParameterField, value: unknown): void {
    const parts = field.key.split('.');
    const last = parts.pop()!;
    let target = this.values;
    for (const part of parts) {
      if (!target[part] || typeof target[part] !== 'object') target[part] = {};
      target = target[part] as Record<string, unknown>;
    }
    target[last] = value === '' && field.nullable ? null : value;
    this.update();
  }
  parameters(): Record<string, unknown> {
    const values = structuredClone(this.values);
    for (const field of this.fields.filter((item) => item.itemType)) {
      const parts = field.key.split('.');
      const last = parts.pop()!;
      const target = parts.reduce((value, part) => value[part] as Record<string, unknown>, values);
      if (typeof target[last] === 'string') target[last] = (target[last] as string).replaceAll('，', ',').split(',').map((part) => part.trim()).filter(Boolean).map((part) => field.itemType === 'string' ? part : Number(part));
    }
    return values;
  }
  render(): React.ReactElement {
    return <>
      <div className="solo-parameter-grid">{this.fields.map((field) => {
        const current = this.value(field.key);
        const value = Array.isArray(current) ? current.join(', ') : String(current ?? '');
        const properties = { key: field.key, value, disabled: this.busy, 'aria-label': field.label };
        if (field.control === 'select') return <div key={field.key} className="solo-select-field">
          <label htmlFor={`solo-${field.key}`}>{field.label}</label><Select {...properties} scale="xsmall" id={`solo-${field.key}`}
          onChange={(event) => this.change(field, field.options?.find((option) => String(option.value) === (event.target as HTMLSelectElement).value)?.value ?? '')}>
          <Option value="" disabled={!field.nullable}>请选择</Option>
          {field.options?.map((option) => <Option key={String(option.value)} value={String(option.value)}>{option.label}</Option>)}
        </Select></div>;
        if (field.control === 'number') return <NumberField {...properties} min={field.min} max={field.max} step={field.step}
          ref={(node) => { if (node && field.step === undefined) { node.proxy.step = 'any'; node.validate(); } }}
          onInput={(event) => { const text = (event.target as HTMLInputElement).value; this.change(field, text === '' ? '' : Number(text)); }}>{field.label}</NumberField>;
        if (field.control === 'date') return <DateField {...properties}
          onInput={(event) => this.change(field, (event.target as HTMLInputElement).value)}>{field.label}</DateField>;
        if (field.control === 'checkbox') return <Checkbox key={field.key} disabled={this.busy} checked={Boolean(current)}
          onChange={(event) => this.change(field, (event.target as HTMLInputElement).checked)}>{field.label}</Checkbox>;
        return <TextField {...properties} onInput={(event) => this.change(field, (event.target as HTMLInputElement).value)}>{field.label}</TextField>;
      })}</div>
      <TextField className="solo-version-note" value={this.note} maxlength={500} disabled={this.busy}
        onInput={(event) => { this.note = (event.target as HTMLInputElement).value; this.update(); }}>版本备注</TextField>
      {this.error && <p className="solo-project-message" role="alert">{this.error}</p>}
      {this.busy && <p role="status">正在校验参数并保存…</p>}
    </>;
  }
}

class SaveDialog extends Dialog<Version> {
  private saving = false;
  constructor(private readonly form: ParameterBody, private readonly project: Project, private readonly saveFiles: () => Promise<void>) {
    super({ title: form.definition.title, body: form, buttons: [Dialog.cancelButton(), Dialog.okButton({ label: '保存版本' })] });
    this.addClass('solo-parameters-dialog');
  }
  resolve(index = 1): void {
    if (this.saving) return;
    if (index === 0) { super.resolve(index); return; }
    this.saving = true;
    this.form.busy = true;
    this.form.error = '';
    this.form.update();
    void this.saveFiles().then(() => request<Version>(this.project.path, {
      parameters: this.form.parameters(), note: this.form.note
    })).then(() => super.resolve(index)).catch((error: Error) => {
      this.form.error = error.message;
    }).finally(() => { this.saving = false; this.form.busy = false; this.form.update(); });
  }
}

export function ProjectVersions({ project, saveFiles }: { project: Project; saveFiles: () => Promise<void> }): React.ReactElement {
  const [version, setVersion] = useState<Version>();
  const [opening, setOpening] = useState(false);
  const [recovering, setRecovering] = useState(false);
  const [error, setError] = useState('');
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    let active = true;
    const load = async (): Promise<void> => {
      try {
        const data = await request<{ versions: Version[] }>(project.path);
        if (active) { setVersion(data.versions[0]); setError(''); }
      } catch (reason) { if (active) setError(String(reason)); }
    };
    void load();
    const timer = setInterval(() => void load(), 5000);
    return () => { active = false; clearInterval(timer); };
  }, [project.path, refresh]);
  async function open(): Promise<void> {
    setOpening(true);
    setError('');
    try {
      await saveFiles();
      const definition = await request<ParameterPanel>(project.path, undefined, true);
      if (definition.protocol !== 2) throw new Error('不支持当前 Scheme 的参数面板协议');
      await new SaveDialog(new ParameterBody(definition), project, saveFiles).launch();
      setRefresh((value) => value + 1);
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setOpening(false); }
  }
  async function recover(action: 'cancel' | 'retry'): Promise<void> {
    if (!version) return;
    setRecovering(true);
    setError('');
    try {
      setVersion(await request<Version>(project.path, { action, version_id: version.id }));
      setRefresh((value) => value + 1);
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setRecovering(false); }
  }
  const labels: Record<string, string> = { building: '正在构建', queued: '等待执行', running: '正在运行', success: '报告已生成', failed: '保存失败', submit_failed: '提交失败' };
  return <>
    <div className="solo-project-actions">
      <Button appearance="accent" disabled={opening || recovering || version?.phase === 'building'} onClick={() => void open()}>
        {opening ? '研究参数…' : '保存版本'}
      </Button>
      {version?.phase === 'building' && <Button disabled={recovering} onClick={() => void recover('cancel')}>取消构建</Button>}
      {version?.phase === 'submit_failed' && <Button disabled={recovering} onClick={() => void recover('retry')}>重试提交</Button>}
      {version?.phase === 'queued' && <Button disabled={recovering} onClick={() => void recover('retry')}>核实提交</Button>}
    </div>
    {version && <p className="solo-project-message" role="status">v{version.number} · {labels[version.phase] ?? version.phase}</p>}
    {(error || version?.error) && <p className="solo-project-message" role="alert">{error || version?.error}</p>}
  </>;
}
