import { useEffect, useState, useSyncExternalStore } from "react";
import { useRuntimeIdentity } from "../components/IdentityGate";
import { CommerceClient } from "../lib/commerceClient";

export function useCommerceAgent() {
  const { identity, runtime, storage } = useRuntimeIdentity();
  const [client] = useState(() => new CommerceClient({
    url: `${runtime.base}/commerce/ag-ui/run`, storage,
    buyerId: identity.buyerId, fetch: runtime.fetch,
  }));
  const snapshot = useSyncExternalStore(client.subscribe, client.getSnapshot);
  useEffect(() => { void client.initialize(); return () => client.detach(); }, [client]);
  useEffect(() => {
    void client.refreshConfirmations();
    void client.refreshSkills();
  }, [client, snapshot.sessionId]);
  return {
    ...snapshot,
    submit: client.submit,
    submitForm: client.submitForm,
    resolveToolApproval: client.resolveToolApproval,
    stop: client.stop,
    reset: client.reset,
    setSession: client.setSession,
    prepareOrder: client.prepareOrder,
    prepareCancel: client.prepareCancel,
    revalidateConfirmation: client.revalidateConfirmation,
    resolveConfirmation: client.resolveConfirmation,
    refreshConfirmations: client.refreshConfirmations,
    refreshSkills: client.refreshSkills,
    refreshHistory: client.refreshHistory,
    workspaceRequest: client.workspaceRequest,
    resume: client.resume,
  };
}
