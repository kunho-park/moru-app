import { useEffect } from "react";

import { initializeTranslationQueue } from "@/stores/translationQueue";

/** Starts persisted queue reconciliation only after EngineGate is ready. */
export function TranslationQueueRuntime() {
  useEffect(() => {
    void initializeTranslationQueue();
  }, []);
  return null;
}
