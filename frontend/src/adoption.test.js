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
