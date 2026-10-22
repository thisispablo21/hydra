// Run with: node tests/memory_dashboard.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const requests = [];
const notices = new Map();
let reply = async () => new Response('{}');
const context = vm.createContext({
    Response, console,
    API: '/api', ensureToken() {},
    apiFetch: async (path, opts) => {
        requests.push({ path, ...opts });
        return reply(path, opts);
    },
    escHtml: value => String(value ?? ''),
    confirm: () => true,
    alert: value => { context.lastAlert = value; },
    document: {
        getElementById: id => {
            if (!notices.has(id)) notices.set(id, {});
            return notices.get(id);
        },
        querySelector: () => null,
        querySelectorAll: () => [{ value: 'demo' }, { value: 'other' }],
    },
});
vm.runInContext(readFileSync(new URL('../static/memory.js', import.meta.url), 'utf8')
    .replace(/ensureToken\(\);\s*refresh\(\);\s*$/, ''), context);
const run = code => vm.runInContext(code, context);
run('render = () => {}; renderTopicEditor = () => {};');

const source = {
    id: 1, name: 'example', body: 'unchanged body', description: 'description',
    type: 'reference', project_slug: null, topics: ['editing'],
};
context.source = source;
run('memories = [{...source}]; topics = [{slug:"editing",title:"Editing",description:"Edit prose"}];');
run('startRouting(1); setRoutingMode("catalog");');
reply = async (_path, opts) => new Response(JSON.stringify({ ...source, ...JSON.parse(opts.body) }));
await run('saveRouting(1)');
assert.deepEqual(JSON.parse(requests.at(-1).body), { topics: [] });
assert.equal(run('memories[0].body'), source.body);
assert.equal(run('routingStatus'), 'Routing saved.');
run('setRoutingMode("unclassified")');
assert.equal(run('routingStatus'), '');
await run('saveRouting(1)');
assert.deepEqual(JSON.parse(requests.at(-1).body), { topics: null });
reply = async () => new Response(JSON.stringify(source));
run('setRoutingMode("catalog")');
await run('saveRouting(1)');
assert.match(run('routingStatus'), /Server did not confirm routing/);
reply = async () => { throw new Error('offline'); };
await run('saveRouting(1)');
assert.match(run('routingStatus'), /Network error: offline/);

run('routingStatus = "old success"; topicStatus = "old success";');
await run('refresh()');
assert.equal(run('memories.length'), 0);
assert.equal(run('topics.length'), 0);
assert.equal(run('routingStatus'), '');
assert.equal(run('topicStatus'), '');
assert.match(run('fetchErrors.Memories'), /offline/);

const catalog = [
    { slug: 'testing', title: 'Testing', description: 'Verify changes' },
    { slug: 'editing', title: 'Editing', description: 'Edit prose' },
];
context.catalog = catalog;
run('fetchErrors = {}; topicDraft = catalog.map(t => ({...t}));');
requests.length = 0;
reply = async (_path, opts) => opts?.method === 'PUT'
    ? new Response('{"status":"ok"}')
    : new Response(JSON.stringify([...catalog].reverse()));
await run('saveTopics()');
assert.equal(requests.length, 2);
assert.equal(requests[0].method, 'PUT');
assert.equal(requests[1].method, undefined);
assert.equal(requests[1].path, '/api/config/memory-topics');
assert.equal(run('topicStatus'), 'Catalog saved.');
assert.equal(run('topics[0].slug'), 'editing');

reply = async (_path, opts) => opts?.method === 'PUT'
    ? new Response('{"status":"ok"}')
    : new Response('{"detail":"readback unavailable"}', { status: 503 });
await run('saveTopics()');
assert.match(run('topicStatus'), /Catalog saved on server, but verification failed/);
assert.match(run('topicStatus'), /Refresh to inspect/);
assert.equal(run('topics.length'), 0);
assert.match(run('fetchErrors["Topic catalog"]'), /readback unavailable/);
assert.equal(run('topicDraft.length'), 2);

run('fetchErrors = {};');
reply = async (_path, opts) => opts?.method === 'PUT'
    ? new Response('{"status":"ok"}')
    : new Response('[]');
await run('saveTopics()');
assert.match(run('topicStatus'), /verification failed.*differs/);
assert.equal(run('topics.length'), 0);

run('fetchErrors = {};');
reply = async () => new Response('{"detail":"referenced topic"}', { status: 409 });
await run('saveTopics()');
assert.match(run('topicStatus'), /^Save failed: referenced topic/);
assert.equal(run('fetchErrors["Topic catalog"]'), undefined);

requests.length = 0;
run('fetchErrors = {}; memories = [{...source}]; projects = [{slug:"demo"},{slug:"other"}];');
reply = async (_path, opts) => {
    if (opts?.method === 'POST') {
        const body = JSON.parse(opts.body);
        return body.project_slug === 'demo'
            ? new Response(JSON.stringify({ ...body, id: 2 }))
            : new Response(JSON.stringify({ detail: 'copy failed' }), { status: 409 });
    }
    if (_path === '/api/projects') return new Response('[{"slug":"demo"},{"slug":"other"}]');
    if (_path === '/api/memory') return new Response(JSON.stringify([source]));
    if (_path === '/api/config/memory-topics') return new Response('[]');
    return new Response('instructions');
};
await run('confirmDistribute(1)');
const copies = requests.filter(request => request.method === 'POST');
assert.equal(copies.length, 2);
for (const copy of copies) assert.deepEqual(JSON.parse(copy.body).topics, ['editing']);
assert.equal(requests.some(request => request.method === 'DELETE'), false);
assert.match(context.lastAlert, /partially failed/);
assert.equal(run('memories[0].id'), 1);
assert.match(run('renderRouting({...source, project_slug:"demo", topics:[]})'), /Project startup index/);
console.log('Dashboard regression checks passed: routing-only PUT, explicit null, unsupported response, network errors, stale refresh state, catalog acknowledgement/readback, successful-PUT verification failures, copy routing, partial copy failure, project override.');
