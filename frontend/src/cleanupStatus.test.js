import test from 'node:test';
import assert from 'node:assert/strict';
import { cleanupStatus } from './cleanupStatus.js';
test('absent records are not successful cleanup',()=>{
  assert.equal(cleanupStatus(undefined),null);assert.equal(cleanupStatus([]),null);
});
test('pending and running remain separate from deployment success',()=>{
  assert.deepEqual(cleanupStatus([{kind:'build-cache',state:'pending',attempts:2}]),{pending:true,label:'Cleanup pending',detail:'Build cache will be retried automatically.'});
  assert.equal(cleanupStatus([{kind:'job-images',state:'succeeded'},{kind:'previous-images',state:'running'}]).label,'Cleanup in progress');
});
test('completion is derived only from observed terminal records',()=>{
  assert.equal(cleanupStatus([{kind:'job-images',state:'succeeded'}]).label,'Recorded cleanup completed');
  assert.equal(cleanupStatus([{kind:'build-cache',state:'unexpected'}]).label,'Cleanup status unavailable');
});
