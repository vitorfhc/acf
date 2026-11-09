// Run with node --test tests/test_file_upload.js. No browser, network, or packages required.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import {createContext, runInContext} from 'node:vm';

const html = readFileSync(new URL('../src/acf/runtime/files.html', import.meta.url), 'utf8');
const source = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)]
    .map(match => match[1]).find(script => script.includes('function submitUpload('));

class Element {
    constructor(tag = 'div') {
        this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {};
        this.attributes = {}; this.handlers = {}; this.value = ''; this.files = [];
        this.hidden = false; this.disabled = false; this.checked = false; this.open = false;
        this.classList = {toggle() {}, contains() { return false; }};
    }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children = items; }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    removeAttribute(name) { delete this.attributes[name]; }
    addEventListener(name, handler) { this.handlers[name] = handler; }
    focus() { this.focused = true; }
    showModal() { this.open = true; }
    close() { this.open = false; }
}

function harness({upload = {ok: true, status: 201, result: {path: '/workspace/reports/report.txt'}}, configError = false, base = ''} = {}) {
    const nodes = new Map(), requests = [], rendered = [];
    const node = id => {
        if (!nodes.has(id)) nodes.set(id, new Element());
        return nodes.get(id);
    };
    const context = createContext({
        URL, Map, Set, console,
        document: {getElementById: node, createElement: tag => new Element(tag),
            createElementNS: (namespace, tag) => Object.assign(new Element(tag), {namespaceURI: namespace}),
            createTextNode: text => ({textContent: text})},
        location: {origin: 'http://files.test:8081', pathname: base + '/browse/workspace/reports', hash: ''},
        history: {pushState() {}},
        setTimeout: () => 1, clearTimeout() {},
        async fetch(url, options = {}) {
            url = new URL(url); requests.push({url, options});
            assert.ok(url.pathname.startsWith(base + '/api/'), `request outside the access token: ${url}`);
            const route = url.pathname.slice(base.length);
            if (options.method === 'POST') {
                const response = typeof upload === 'function' ? await upload(url, options) : upload;
                return {ok: response.ok, status: response.status, json: async () => response.result};
            }
            if (route === '/api/config') {
                if (configError) return {ok: false, status: 503, json: async () => ({error: 'Settings unavailable'})};
                return {ok: true, json: async () => ({home: '/root', downloads: '/root/Downloads'})};
            }
            assert.equal(route, '/api/stat');
            return {ok: true, json: async () => ({kind: url.searchParams.get('path').endsWith('.txt') ? 'file' : 'directory'})};
        },
    });
    context.window = context;
    runInContext(source.slice(0, source.indexOf("$('brand-icon')")), context);
    context.renderDirectory = async path => rendered.push(path);
    context.renderFile = async path => rendered.push(path);
    context.breadcrumbs = () => {};
    context.highlightTree = () => {};
    context.sidebar = () => {};
    context.scrollToMarkdownAnchor = () => {};
    let prevented = 0;
    return {context, node, requests, rendered,
        posts: () => requests.filter(request => request.options.method === 'POST'),
        rows: () => node('upload-queue').children,
        names: () => node('upload-queue').children.map(row => row.children[1].children[1]),
        statuses: () => node('upload-queue').children.map(row => row.children[2]),
        event: {preventDefault() { prevented++; }},
        prevented: () => prevented,
        async prepare(path = '/workspace/reports') {
            await context.loadFileSettings(); await context.navigate(path); context.openUpload();
        },
        select(name = 'report.txt', size = 3) {
            const file = {name, size}; node('upload-file').files = [file]; context.uploadFileChanged(); return file;
        },
        selectMany(files) { node('upload-file').files = files; context.uploadFileChanged(); return files; },
    };
}

test('upload dialog and Downloads shortcut make the destination visible before any write', async () => {
    const app = harness();
    await app.prepare();
    assert.equal(app.node('upload-dialog').open, true);
    assert.equal(app.node('upload-destination').textContent, '/workspace/reports');
    assert.equal(app.node('upload-downloads').checked, false);
    assert.equal(app.node('upload-submit').disabled, true);
    assert.match(app.node('upload-note').textContent, /Existing files will not be overwritten/);
    app.select();
    app.node('upload-downloads').checked = true;
    app.context.updateUploadDestination();
    assert.equal(app.node('upload-destination').textContent, '/root/Downloads');
    assert.equal(app.posts().length, 0);
    const downloads = app.node('shortcuts').children.find(link => link.dataset.path === '/root/Downloads');
    assert.equal(downloads.href, '/browse/root/Downloads');
    assert.ok(downloads.children.some(child => child.textContent === 'Downloads'));
    app.context.closeUpload();
    assert.equal(app.posts().length, 0);
    app.context.openUpload();
    assert.equal(app.node('upload-downloads').checked, false);
    assert.equal(app.node('upload-destination').textContent, '/workspace/reports');
});

test('viewing a file uploads into its parent directory, with literal filename and raw body', async () => {
    const app = harness({upload: {ok: true, status: 201, result: {path: '/workspace/reports/a & # é.txt'}}});
    await app.prepare('/workspace/reports/existing.txt');
    assert.equal(app.node('upload-destination').textContent, '/workspace/reports');
    const file = app.select('a & # é.txt', 0);
    await app.context.submitUpload(app.event);
    assert.equal(app.prevented(), 1);
    const request = app.posts()[0];
    assert.equal(request.url.pathname, '/api/upload');
    assert.equal(request.url.searchParams.get('path'), '/workspace/reports');
    assert.equal(request.url.searchParams.get('name'), 'a & # é.txt');
    assert.equal(request.options.body, file);
    assert.equal(request.options.headers['Content-Type'], 'application/octet-stream');
    assert.equal(request.options.headers['X-ACF-Upload'], '1');
    assert.equal(app.node('upload-dialog').open, false);
    assert.equal(app.rendered.at(-1), '/workspace/reports');
    assert.equal(app.node('toast').textContent, 'Uploaded to /workspace/reports/a & # é.txt');
});

test('Downloads checkbox changes the actual upload destination and resulting folder', async () => {
    const app = harness({upload: {ok: true, status: 201, result: {path: '/root/Downloads/report.txt'}}});
    await app.prepare(); app.select();
    app.node('upload-downloads').checked = true; app.context.updateUploadDestination();
    await app.context.submitUpload(app.event);
    assert.equal(app.posts()[0].url.searchParams.get('path'), '/root/Downloads');
    assert.equal(app.rendered.at(-1), '/root/Downloads');
});

test('conflicts keep the dialog open and permit renaming without overwriting', async () => {
    const app = harness({upload: {ok: false, status: 409, result: {error: 'A file with this name already exists'}}});
    await app.prepare(); app.select();
    await app.context.submitUpload(app.event);
    assert.equal(app.node('upload-dialog').open, true);
    assert.equal(app.node('upload-error').hidden, false);
    assert.match(app.statuses()[0].textContent, /already exists/);
    assert.equal(app.names()[0].value, 'report.txt');
    assert.equal(app.names()[0].disabled, false);
    assert.equal(app.node('upload-cancel').disabled, false);
    assert.equal(app.node('upload-progress').hidden, true);
    app.names()[0].value = 'report-copy.txt';
    await app.context.submitUpload(app.event);
    assert.equal(app.posts()[1].url.searchParams.get('name'), 'report-copy.txt');
});

test('invalid filenames fail before making an upload request', async () => {
    for (const [name, bytes] of [['../escape', 3], ['folder\\escape', 3], ['..', 3]]) {
        const app = harness(); await app.prepare(); app.select(name, bytes);
        await app.context.submitUpload(app.event);
        assert.equal(app.posts().length, 0);
        assert.equal(app.node('upload-error').hidden, false);
        assert.equal(app.node('upload-dialog').open, true);
    }
});

test('upload controls stay disabled while sending and duplicate submissions are ignored', async () => {
    let release;
    const app = harness({upload: () => new Promise(resolve => { release = resolve; })});
    await app.prepare(); app.select();
    const pending = app.context.submitUpload(app.event);
    assert.equal(app.node('upload-progress').hidden, false);
    for (const id of ['upload-file', 'upload-downloads', 'upload-submit', 'upload-cancel']) {
        assert.equal(app.node(id).disabled, true, id);
    }
    assert.equal(app.names()[0].disabled, true);
    app.context.closeUpload();
    assert.equal(app.node('upload-dialog').open, true);
    await app.context.submitUpload(app.event);
    assert.equal(app.posts().length, 1);
    release({ok: true, status: 201, result: {path: '/workspace/reports/report.txt'}});
    await pending;
    assert.equal(app.node('upload-dialog').open, false);
});

test('unavailable settings leave browsing usable but prevent upload', async () => {
    const app = harness({configError: true});
    await app.prepare();
    assert.equal(app.node('upload-open').disabled, true);
    assert.equal(app.node('upload-dialog').open, false);
    assert.equal(app.rendered.at(-1), '/workspace/reports');
    assert.equal(app.posts().length, 0);
    assert.match(app.node('toast').textContent, /Uploads unavailable/);
});


test('multiple files upload sequentially without a size cap', async () => {
    let active = 0, peak = 0;
    const app = harness({upload: async url => {
        active++; peak = Math.max(peak, active);
        await Promise.resolve(); active--;
        return {ok: true, status: 201, result: {path: url.searchParams.get('path')+'/'+url.searchParams.get('name')}};
    }});
    await app.prepare();
    const files = app.selectMany([{name: 'large.bin', size: 5 * 1024 ** 3}, {name: 'empty.txt', size: 0}, {name: 'notes.txt', size: 10}]);
    assert.match(html, /id="upload-file" multiple/);
    assert.equal(app.rows().length, 3);
    assert.equal(app.node('upload-submit').textContent, 'Upload 3 files');
    assert.doesNotMatch(app.node('upload-note').textContent, /limit|up to/i);
    await app.context.submitUpload(app.event);
    assert.equal(app.posts().length, 3);
    assert.equal(peak, 1);
    assert.deepEqual(app.posts().map(request => request.options.body), files);
    assert.equal(app.node('upload-dialog').open, false);
    assert.equal(app.node('toast').textContent, 'Uploaded 3 files to /workspace/reports');
});

test('partial failures keep successful files, attempt all files, and retry only remaining names', async () => {
    const app = harness({upload: async url => {
        const name = url.searchParams.get('name');
        return name === 'conflict.txt'
            ? {ok: false, status: 409, result: {error: 'A file with this name already exists'}}
            : {ok: true, status: 201, result: {path: '/workspace/reports/'+name}};
    }});
    await app.prepare();
    app.selectMany([{name: 'first.txt', size: 1}, {name: 'conflict.txt', size: 2}, {name: 'last.txt', size: 3}]);
    await app.context.submitUpload(app.event);
    assert.deepEqual(app.posts().map(request => request.url.searchParams.get('name')), ['first.txt', 'conflict.txt', 'last.txt']);
    assert.equal(app.node('upload-dialog').open, true);
    assert.match(app.node('upload-error').textContent, /2 of 3 files uploaded/);
    assert.equal(app.node('upload-submit').textContent, 'Retry remaining (1)');
    assert.equal(app.node('upload-file').disabled, true);
    assert.equal(app.node('upload-downloads').disabled, true);
    assert.deepEqual(app.names().map(input => input.disabled), [true, false, true]);
    assert.deepEqual(app.rows().map(row => row.className), ['upload-item uploaded', 'upload-item failed', 'upload-item uploaded']);
    app.names()[1].value = 'renamed.txt'; app.names()[1].handlers.input();
    await app.context.submitUpload(app.event);
    assert.deepEqual(app.posts().map(request => request.url.searchParams.get('name')), ['first.txt', 'conflict.txt', 'last.txt', 'renamed.txt']);
    assert.equal(app.node('upload-dialog').open, false);
    assert.equal(app.node('toast').textContent, 'Uploaded 3 files to /workspace/reports');
});

test('an invalid name does not stop later valid files in the same batch', async () => {
    const app = harness(); await app.prepare();
    app.selectMany([{name: '../invalid.txt', size: 1}, {name: 'valid.txt', size: 2}]);
    await app.context.submitUpload(app.event);
    assert.equal(app.posts().length, 1);
    assert.equal(app.posts()[0].url.searchParams.get('name'), 'valid.txt');
    assert.match(app.statuses()[0].textContent, /without folders or slashes/);
    assert.equal(app.statuses()[1].textContent, 'Uploaded');
    assert.equal(app.node('upload-dialog').open, true);
});

test('uploads, settings, and shortcuts stay below the access token path prefix', async () => {
    const base = '/fixture-access-token-0123456789abcdefABCDEF';
    const app = harness({base});
    await app.prepare();
    assert.equal(app.node('upload-destination').textContent, '/workspace/reports');
    const downloads = app.node('shortcuts').children.find(link => link.dataset.path === '/root/Downloads');
    assert.equal(downloads.href, base + '/browse/root/Downloads');
    app.select();
    await app.context.submitUpload(app.event);
    assert.equal(app.posts()[0].url.pathname, base + '/api/upload');
    assert.equal(app.posts()[0].url.searchParams.get('path'), '/workspace/reports');
});
