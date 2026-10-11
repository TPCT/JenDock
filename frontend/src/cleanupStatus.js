const names = {
  'job-images': 'Unused build images',
  'previous-images': 'Previous images',
  'build-cache': 'Build cache',
  'legacy-images': 'Previous deployment images',
};
export function cleanupStatus(records) {
  if (!Array.isArray(records) || !records.length) return null;
  if (records.some(item => !item || !names[item.kind] || !['pending', 'running', 'succeeded'].includes(item.state))) {
    return { pending: true, label: 'Cleanup status unavailable', detail: 'Deployment status is unchanged.' };
  }
  const pending = records.filter(item => item.state !== 'succeeded');
  if (!pending.length) return { pending: false, label: 'Recorded cleanup completed', detail: 'Protected images and persistent data are retained.' };
  const running = pending.some(item => item.state === 'running');
  return {
    pending: true,
    label: running ? 'Cleanup in progress' : 'Cleanup pending',
    detail: running
      ? 'Cleanup is running for: ' + pending.map(item => names[item.kind]).join(', ') + '.'
      : pending.map(item => names[item.kind]).join(', ') + ' will be retried automatically.',
  };
}
