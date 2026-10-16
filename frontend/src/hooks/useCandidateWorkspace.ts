import { useEffect, useMemo, useSyncExternalStore } from "react";
import { CandidateWorkspaceController, type WorkspaceRequest } from "../lib/candidateWorkspace";

export function useCandidateWorkspace(buyerId: string, sessionId: string, request: WorkspaceRequest, refreshKey: string) {
  const controller = useMemo(() => {
    let storage: Storage | undefined;
    try { storage = window.sessionStorage; } catch { /* 可读服务器集合，写入时持续提示恢复限制。 */ }
    return new CandidateWorkspaceController(buyerId, sessionId, request, storage);
  }, [buyerId, sessionId, request]);
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
  useEffect(() => {
    controller.start();
    const refresh = () => { void controller.refresh(); };
    const visible = () => { if (document.visibilityState === "visible") refresh(); };
    window.addEventListener("focus", refresh); window.addEventListener("online", refresh);
    document.addEventListener("visibilitychange", visible);
    return () => { controller.dispose(); window.removeEventListener("focus", refresh); window.removeEventListener("online", refresh); document.removeEventListener("visibilitychange", visible); };
  }, [controller]);
  useEffect(() => { void controller.refresh(); }, [controller, refreshKey]);
  return { ...state, controller };
}
