import { useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";
import { describeError, type Api, type JobRecord } from "./api";

export function useRemote<T>(api: Api, path: string, keepPolling?: (value: T) => boolean) {
  const [generation, setGeneration] = useState(0);
  const [state, setState] = useState<{ data: T | null; error: string }>({ data: null, error: "" });
  const reload = useCallback(() => setGeneration((value) => value + 1), []);
  useEffect(() => {
    const controller = new AbortController();
    let timer: number | undefined;
    setState((current) => ({ ...current, error: "" }));
    async function load() {
      try {
        const data = await api.json<T>(path, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setState({ data, error: "" });
        if (keepPolling?.(data)) timer = window.setTimeout(load, 900);
      } catch (error) {
        if (!controller.signal.aborted) setState((current) => ({ ...current, error: describeError(error) }));
      }
    }
    void load();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [api, path, keepPolling, generation]);
  return { ...state, reload };
}

export function Problem({ children, retry }: { children: string; retry?: () => void }) {
  return <div className="notice error" role="alert">
    <span>{children}</span>
    {retry && <button className="text-button" onClick={retry}>重试</button>}
  </div>;
}

export function Empty({ title, children }: { title: string; children: ReactNode }) {
  return <div className="empty"><strong>{title}</strong><div>{children}</div></div>;
}

export function Progress({ record, type }: { record: JobRecord; type: "report" | "practice" }) {
  return <div className="notice progress" role="status">
    <span className="working-dot" aria-hidden="true" />
    <div><strong>{record.status === "queued" ? "正在排队" : type === "report" ? "正在识别并核验题目" : "正在生成并核验练习"}</strong>
      <p>可以先离开这个页面，稍后从记录中继续查看。</p></div>
  </div>;
}

export function Icon({ name, size = 20 }: { name: "mark" | "upload" | "arrow" | "image" | "layers"; size?: number }) {
  const line = {
    mark: <><path d="M6 3h9l4 4v14H6zM15 3v5h4"/><path d="m8.5 14 2 2 4.5-5"/></>,
    upload: <><path d="M12 16V4m-5 5 5-5 5 5M4 15v5h16v-5"/></>,
    arrow: <path d="M4 12h15m-6-6 6 6-6 6"/>,
    image: <><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8.5" cy="9" r="1.5"/><path d="m3 17 5-4 4 3 4-5 5 5"/></>,
    layers: <><path d="m12 3 9 5-9 5-9-5 9-5zm-9 9 9 5 9-5M3 16l9 5 9-5"/></>,
  }[name];
  return <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none"
    stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">{line}</svg>;
}

export const dateText = (timestamp: number) => new Intl.DateTimeFormat("zh-CN", {
  month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit",
}).format(timestamp);

export const statusText = { queued: "排队中", running: "处理中", done: "可查看", failed: "未完成" };
