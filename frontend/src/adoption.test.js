import test from 'node:test';
import assert from 'node:assert/strict';
import {selectionPayload,reviewedSelection,matchesReview,linkedEnvironment} from './adoption.js';
test('review binds exact environment recipe and service set',()=>{
 const selection={recipe_revision:7,services:['queue','app']};
 const preview={project:'app',environment:'production',selected_recipe_revision:7,selected_services:['app','queue']};
 assert.equal(matchesReview(preview,'app','production',selection),true);
 assert.equal(matchesReview(preview,'app','development',selection),false);
 assert.equal(matchesReview(preview,'app','production',{...selection,recipe_revision:8}),false);
 assert.equal(matchesReview(preview,'app','production',{...selection,services:['app']}),false);
 assert.deepEqual(reviewedSelection(preview),selectionPayload(selection));
 assert.deepEqual(selection.services,['queue','app']);
 assert.deepEqual(selectionPayload({}),{});
});
test('second environment requires exact receipt not first legacy anchor',()=>{
 const fingerprint='a'.repeat(64);
 const result={project:{id:'app',legacy_binding:'development',environments:{production:{recipe_revision:9,state:'active'}}},receipt:{project:'app',environment:'production',fingerprint}};
 assert.equal(linkedEnvironment(result,'app','production',fingerprint),true);
 assert.equal(linkedEnvironment(result,'app','production','b'.repeat(64)),false);
 assert.equal(linkedEnvironment({...result,receipt:undefined},'app','production',fingerprint),false);
 assert.equal(linkedEnvironment(result,'app','development',fingerprint),false);
});

import {adoptionRecoveryKey,adoptionCheckpoint,restoreAdoption} from './adoption.js';
test('recovery persists only exact request identity and isolates owner and environment',()=>{
 const preview={project:'app',environment:'production',fingerprint:'a'.repeat(64),selected_recipe_revision:7,selected_services:['queue','app'],databases:[{password:'must-not-persist'}],containers:[{id:'private'}]};
 const saved=adoptionCheckpoint(preview);
 assert.deepEqual(Object.keys(saved).sort(),['environment','fingerprint','project','recipe_revision','services']);
 const key=adoptionRecoveryKey('admin','app','production');
 assert.notEqual(key,adoptionRecoveryKey('other','app','production'));
 assert.notEqual(key,adoptionRecoveryKey('admin','app','development'));
 const storage={getItem:()=>JSON.stringify(saved)};
 assert.deepEqual(restoreAdoption(storage,key,'app','production'),saved);
 assert.throws(()=>restoreAdoption(storage,key,'app','development'));
 assert.equal(restoreAdoption({getItem:()=>null},key,'app','production'),null);
});
test('malformed or unreadable recovery fails closed',()=>{
 const value={project:'app',environment:'production',fingerprint:'a'.repeat(64)};
 for(const bad of ['{',JSON.stringify({...value,fingerprint:'bad'}),JSON.stringify({...value,services:['app','app']}),JSON.stringify({...value,recipe_revision:-1})]){
  assert.throws(()=>restoreAdoption({getItem:()=>bad},'key','app','production'));
 }
 assert.throws(()=>restoreAdoption({getItem:()=>{throw new Error('denied')}},'key','app','production'));
});
