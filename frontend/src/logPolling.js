// One request at a time. Pausing cancels scheduled work, not the displayed data.
export function createLogPolling({
  fetchLogs,
  onResult,
  onError,
  onLoading,
  live = true,
  schedule = setTimeout,
  cancel = clearTimeout,
}) {
  let active = true,
    polling = live,
    running = false,
    timer;
  const clear = () => {
    if (timer !== undefined) cancel(timer);
    timer = undefined;
  };
  const fetch = async () => {
    if (!active || running) return;
    clear();
    running = true;
    onLoading(true);
    try {
      const result = await fetchLogs();
      if (active) onResult(result);
    } catch (error) {
      if (active) onError(error);
    } finally {
      running = false;
      if (active) {
        onLoading(false);
        if (polling) timer = schedule(fetch, 5000);
      }
    }
  };
  fetch();
  return {
    reload: fetch,
    setLive(value) {
      if (polling === value) return;
      polling = value;
      clear();
      if (polling) fetch();
    },
    stop() {
      active = false;
      clear();
    },
  };
}
