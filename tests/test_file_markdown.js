// Run with node --test tests/test_file_markdown.js.
// Exercise the shipped UI and Markdown parser without browser or npm dependencies.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import {createContext, runInContext} from 'node:vm';

const runtime = new URL('../src/acf/runtime/', import.meta.url);

class Element {
    constructor(tag) {
        this.tagName = tag.toUpperCase();
        this.childNodes = [];
        this.attributes = new Map();
        this.dataset = {};
        this.handlers = {};
        this.style = {};
        this.hidden = false;
        this.className = '';
        this.classList = {
            contains: name => this.className.split(/\s+/).includes(name),
            toggle: (name, force) => {
                const values = new Set(this.className.split(/\s+/).filter(Boolean));
                const include = force ?? !values.has(name);
                if (include) values.add(name); else values.delete(name);
                this.className = [...values].join(' ');
                return include;
            },
        };
    }
    set textContent(value) { this.childNodes = []; this.text = String(value); }
    get textContent() { return this.text ?? this.childNodes.map(child => child.textContent).join(''); }
    set innerHTML(value) { this.childNodes = []; this.html = String(value); }
    get innerHTML() { return this.html ?? ''; }
    get children() { return this.childNodes.filter(child => child.tagName !== '#TEXT'); }
    get lastChild() { return this.childNodes.at(-1); }
    append(...children) {
        this.text = undefined;
        for (const child of children) {
            child.parentNode = this;
            this.childNodes.push(child);
        }
    }
    replaceChildren(...children) { this.childNodes = []; this.text = undefined; this.append(...children); }
    setAttribute(name, value) { this.attributes.set(name, String(value)); }
    getAttribute(name) { return this.attributes.get(name) ?? null; }
    removeAttribute(name) { this.attributes.delete(name); }
    addEventListener(name, handler) { this.handlers[name] = handler; }
    focus() {}
    remove() {
        if (this.parentNode) this.parentNode.childNodes = this.parentNode.childNodes.filter(child => child !== this);
    }
}

function descendants(root, predicate) {
    return [root, ...root.childNodes.flatMap(child => descendants(child, () => true))].filter(predicate);
}

function harness({storage = new Map(), preview = {}, completeText = '# Complete file\n', base = ''} = {}) {
    const html = readFileSync(new URL('files.html', runtime), 'utf8');
    const script = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)]
        .map(match => match[1]).find(source => source.includes('function renderFile('));
    assert.ok(script, 'the file explorer inline UI script is present');
    const bootstrap = script.indexOf("$('brand-icon')");
    assert.ok(bootstrap > 0, 'UI bootstrap follows function definitions');
    const nodes = new Map();
    const node = id => {
        if (!nodes.has(id)) nodes.set(id, new Element('div'));
        return nodes.get(id);
    };
    const requests = [], clipboard = [];
    const result = {text: '# A report\n', size: 11, mtime: 0, binary: false,
        truncated: false, copyable: true, ...preview};
    const context = createContext({
        console, URL, Map, Set,
        document: {
            getElementById: node,
            createElement: tag => new Element(tag),
            createElementNS: (namespace, tag) => Object.assign(new Element(tag), {namespaceURI: namespace}),
            createTextNode: text => Object.assign(new Element('#text'), {text}),
            body: new Element('body'),
        },
        location: {pathname: base + '/browse/home/dev/project/report.md', origin: 'http://files.test:8090'},
        localStorage: {getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value)},
        navigator: {clipboard: {async writeText(text) { clipboard.push(text); }}},
        isSecureContext: true,
        setTimeout: () => 1,
        clearTimeout: () => {},
        async fetch(url) {
            const request = new URL(url);
            requests.push(request);
            assert.ok(request.pathname.startsWith(base + '/api/'), `request outside the access token: ${request}`);
            const route = request.pathname.slice(base.length);
            assert.ok(['/api/preview', '/api/text'].includes(route), `unexpected request: ${request}`);
            return {ok: true, async json() { return route === '/api/text' ? {text: completeText} : result; }};
        },
    });
    context.window = context;
    const parserSource = html.match(/<script\s+src="([^"?]*markdown-it[^"?]*)"/);
    assert.ok(parserSource, 'the UI loads its locally vendored Markdown parser');
    const parserFile = parserSource[1].split('/').pop();
    runInContext(readFileSync(new URL('vendor/' + parserFile, runtime), 'utf8'), context);
    runInContext(script.slice(0, bootstrap), context);
    return {context, nodes, node, requests, clipboard, result, storage,
        render: (path = '/home/dev/project/report.md') => context.renderFile(path, {}, 0),
        markdown: (text, path = '/home/dev/project/report.md') => context.renderMarkdown(text, path)};
}

test('Markdown renders headings, lists, tables, quotes, and code blocks', () => {
    const app = harness();
    const rendered = app.markdown('# Findings\n\n## Access checks\n\n- **Confirmed** issue\n- Another item\n\n1. Sign in\n2. Replay\n\n> Evidence matters.\n\n| Role | Result |\n| --- | --- |\n| Viewer | Denied |\n\n```js\nconst value = "<script>";\n```\n');
    for (const tag of ['h1', 'h2', 'ul', 'ol', 'blockquote', 'table', 'thead', 'tbody', 'pre', 'code', 'strong']) {
        assert.match(rendered, new RegExp('<' + tag + '(?:\\s|>)'), `${tag} should be rendered`);
    }
    assert.match(rendered, /&lt;script&gt;/);
    assert.match(rendered, /language-js/);
    assert.doesNotMatch(rendered, /<script>/);
});

test('untrusted Markdown cannot create active HTML, script URLs, or automatic image requests', () => {
    const app = harness();
    const rendered = app.markdown([
        '<script>alert(1)</script>',
        '<img src="https://tracker.example/track" onerror="alert(1)">',
        '<iframe src="https://tracker.example/frame"></iframe>',
        '[script](javascript:alert%281%29)',
        '[encoded](java&#x73;cript:alert%281%29)',
        '[data](data:text/html;base64,PHNjcmlwdD4=)',
        '[file](file:///etc/passwd)',
        '![remote image](https://tracker.example/image.png)',
        '![local image](screenshots/evidence.png)',
        '![data image](data:image/png;base64,AAAA)',
    ].join('\n\n'));
    assert.match(rendered, /&lt;script&gt;/);
    assert.doesNotMatch(rendered, /<(?:script|img|iframe|object|embed|svg)\b/i);
    assert.doesNotMatch(rendered, /(?:href|src)="\s*(?:javascript|data|file|vbscript):/i);
    assert.equal(app.requests.length, 0, 'rendering a document must not make network requests');
});

test('relative links navigate the filesystem and external links have safe new-tab attributes', () => {
    const app = harness();
    const rendered = app.markdown('[Sibling](other.md) [Parent](../scope/rules.md) [Space](<notes with spaces.md>) [Root](/etc/hosts) [Web](https://example.com/docs)');
    assert.match(rendered, /href="\/browse\/home\/dev\/project\/other\.md"/);
    assert.match(rendered, /href="\/browse\/home\/dev\/scope\/rules\.md"/);
    assert.match(rendered, /href="\/browse\/home\/dev\/project\/notes%20with%20spaces\.md"/);
    assert.match(rendered, /href="\/browse\/etc\/hosts"/);
    const external = rendered.match(/<a\b[^>]*href="https:\/\/example\.com\/docs"[^>]*>/)?.[0];
    assert.ok(external, 'external HTTPS link remains clickable');
    assert.match(external, /target="_blank"/);
    assert.match(external, /rel="[^"]*noopener/);
    assert.match(external, /rel="[^"]*noreferrer/);
});

test('heading links target unique document anchors and local links cannot carry access tokens', () => {
    const app = harness();
    const rendered = app.markdown('# Access & roles\n\n## Access & roles\n\n[Here](#access-roles) [Next](../README.md?token=untrusted#access-roles) [Absolute](http://files.test:8090/browse/etc/hosts?token=untrusted)');
    assert.match(rendered, /id="markdown-access-roles"/);
    assert.match(rendered, /id="markdown-access-roles-1"/);
    assert.match(rendered, /href="#markdown-access-roles"/);
    assert.match(rendered, /href="\/browse\/home\/dev\/README\.md#markdown-access-roles"/);
    assert.match(rendered, /href="\/browse\/etc\/hosts"/);
    assert.doesNotMatch(rendered, /token=|untrusted/);
    const repeated = app.markdown('# Finding\n\n# Finding\n\n# Finding-1\n');
    const ids = [...repeated.matchAll(/<h1\b[^>]*id="([^"]+)"/g)].map(match => match[1]);
    assert.equal(ids.length, 3);
    assert.equal(new Set(ids).size, 3, 'repeated headings must retain distinct anchor targets');
});

test('Rendered and Raw buttons switch exact source views and preserve the selected mode', async () => {
    const source = '# A report\n\n**Keep this exact source.**\n';
    const app = harness({preview: {text: source}});
    await app.render();
    const content = app.node('content');
    const buttons = descendants(content, node => node.tagName === 'BUTTON');
    const raw = buttons.find(node => node.textContent === 'Raw');
    const rendered = buttons.find(node => node.textContent === 'Rendered');
    assert.ok(raw && rendered, 'Markdown offers both view modes');
    assert.equal(rendered.getAttribute('aria-pressed'), 'true');
    const pre = descendants(content, node => node.tagName === 'PRE')[0];
    const article = descendants(content, node => node.tagName === 'ARTICLE')[0];
    assert.ok(pre && article);
    assert.equal(pre.textContent, source);
    assert.equal(pre.hidden, true);
    assert.equal(article.hidden, false);
    assert.match(article.innerHTML, /<h1\b/);
    await raw.handlers.click();
    assert.equal(raw.getAttribute('aria-pressed'), 'true');
    assert.equal(rendered.getAttribute('aria-pressed'), 'false');
    assert.equal(pre.hidden, false);
    assert.equal(article.hidden, true);
    const reload = harness({storage: app.storage, preview: {text: source}});
    await reload.render('/home/dev/project/another.markdown');
    const selected = descendants(reload.node('content'), node => node.tagName === 'BUTTON' && node.textContent === 'Raw')[0];
    assert.equal(selected.getAttribute('aria-pressed'), 'true', 'the selected mode survives another file and page reload');
    await rendered.handlers.click();
    assert.equal(pre.hidden, true);
    assert.equal(article.hidden, false);
});

test('storage restrictions and an unavailable parser still leave the source readable', async () => {
    const restricted = harness();
    restricted.context.localStorage = {
        getItem() { throw new Error('storage denied'); },
        setItem() { throw new Error('storage denied'); },
    };
    await restricted.render('/home/dev/project/NOTES.MD');
    const raw = descendants(restricted.node('content'), node => node.tagName === 'BUTTON' && node.textContent === 'Raw')[0];
    assert.ok(raw, 'uppercase Markdown extensions also receive view controls');
    await raw.handlers.click();
    assert.equal(raw.getAttribute('aria-pressed'), 'true');

    const unavailable = harness({preview: {text: '# Still readable\n'}});
    unavailable.context.markdownit = undefined;
    await unavailable.render();
    const content = unavailable.node('content');
    const pre = descendants(content, node => node.tagName === 'PRE')[0];
    const rendered = descendants(content, node => node.tagName === 'BUTTON' && node.textContent === 'Rendered')[0];
    assert.equal(pre.hidden, false);
    assert.equal(pre.textContent, '# Still readable\n');
    assert.equal(rendered.disabled, true);
    assert.match(content.textContent, /Showing the raw file/);
});

test('the access token path prefix is kept for links, API requests, and downloads', async () => {
    const base = '/fixture-access-token-0123456789abcdefABCDEF';
    const app = harness({base});
    const rendered = app.markdown('[Sibling](other.md) [Root](/etc/hosts) [Absolute](http://files.test:8090' + base + '/browse/etc/passwd) [Tokenless](http://files.test:8090/browse/etc/group)');
    assert.match(rendered, new RegExp('href="' + base + '/browse/home/dev/project/other\\.md"'));
    assert.match(rendered, new RegExp('href="' + base + '/browse/etc/hosts"'));
    assert.match(rendered, new RegExp('href="' + base + '/browse/etc/passwd"'));
    assert.doesNotMatch(rendered, /href="\/browse\//, 'every local link stays below the token');
    await app.render('/home/dev/project/report.md');
    const content = app.node('content');
    const copy = descendants(content, node => node.tagName === 'BUTTON' && node.textContent === 'Copy contents')[0];
    await copy.handlers.click();
    assert.ok(app.requests.length && app.requests.every(url => url.pathname.startsWith(base + '/api/')));
    const download = descendants(content, node => node.tagName === 'A' && node.textContent === 'Download')[0];
    assert.equal(download.href, base + '/download/home/dev/project/report.md');
    assert.equal(app.context.currentPath(), '/home/dev/project/report.md');
});

test('copy and download preserve the original full source, including a truncated rendered preview', async () => {
    const completeText = '# Complete report\n\n' + '* evidence\n'.repeat(100);
    const path = '/home/dev/project/notes & report.md';
    const app = harness({completeText, preview: {text: '# Complete report\n', truncated: true}});
    await app.render(path);
    const content = app.node('content');
    const copy = descendants(content, node => node.tagName === 'BUTTON' && node.textContent === 'Copy contents')[0];
    assert.ok(copy && !copy.disabled);
    await copy.handlers.click();
    assert.deepEqual(app.clipboard, [completeText]);
    const request = app.requests.find(url => url.pathname === '/api/text');
    assert.equal(request.searchParams.get('path'), path);
    const download = descendants(content, node => node.tagName === 'A' && node.textContent === 'Download')[0];
    assert.equal(download.href, '/download/home/dev/project/notes%20%26%20report.md');
    assert.match(content.textContent, /Preview limited/);
});

test('other text and binary files keep the existing preview behavior', async () => {
    const plain = harness({preview: {text: '# This stays plain text\n'}});
    await plain.render('/home/dev/project/notes.txt');
    assert.equal(descendants(plain.node('content'), node => node.tagName === 'ARTICLE').length, 0);
    assert.equal(descendants(plain.node('content'), node => node.tagName === 'PRE')[0].textContent, '# This stays plain text\n');
    const binary = harness({preview: {binary: true, copyable: false, text: null}});
    await binary.render('/home/dev/project/binary.md');
    assert.equal(descendants(binary.node('content'), node => node.tagName === 'ARTICLE').length, 0);
    const copy = descendants(binary.node('content'), node => node.tagName === 'BUTTON' && node.textContent === 'Copy contents')[0];
    assert.equal(copy.disabled, true);
    assert.match(binary.node('content').textContent, /Binary file/);
});
