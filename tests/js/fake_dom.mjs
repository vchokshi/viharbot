// A tiny DOM, just enough to run ViharBot's page script offline in Node.
// It is deliberately strict: assigning innerHTML (or similar) throws, so a test fails if the
// page ever builds markup from strings.

class TextNode {
  constructor(text) { this.nodeType = 3; this.text = String(text); this.parent = null; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); this.parent = null; }
  get textContent() { return this.text; }
}

let active = null;
export const getActive = () => active;

export class Element {
  constructor(tag, namespace = null) {
    this.nodeType = 1;
    this.tagName = tag.toLowerCase();
    this.namespace = namespace;
    this.children = [];
    this.parent = null;
    this.attrs = {};
    this.className = "";
    this.style = {};
    this.listeners = {};
    this.value = "";
    this.disabled = false;
    this.hidden = false;
    this.scrollTop = 0;
    this.scrollHeight = 0;
  }
  append(...nodes) {
    for (const node of nodes) {
      const child = typeof node === "string" ? new TextNode(node) : node;
      if (child.parent) child.parent.children = child.parent.children.filter((c) => c !== child);
      child.parent = this;
      this.children.push(child);
    }
  }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); this.parent = null; }
  get lastChild() { return this.children[this.children.length - 1] ?? null; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  getAttribute(name) { return this.attrs[name] ?? null; }
  removeAttribute(name) { delete this.attrs[name]; }
  addEventListener(type, handler) { this.listeners[type] = handler; }
  focus() { active = this; }
  requestSubmit() {}
  querySelector(selector) { return findAll(this, selector)[0] ?? null; }
  querySelectorAll(selector) { return findAll(this, selector); }
  get textContent() { return this.children.map((c) => c.textContent).join(""); }
  set textContent(value) {
    this.children.forEach((c) => { c.parent = null; });
    this.children = [];
    if (value !== "" && value !== undefined && value !== null) this.append(new TextNode(value));
  }
  set innerHTML(_) { throw new Error("innerHTML is forbidden: build DOM nodes instead"); }
  get innerHTML() { throw new Error("innerHTML is forbidden"); }
  set outerHTML(_) { throw new Error("outerHTML is forbidden"); }
  insertAdjacentHTML() { throw new Error("insertAdjacentHTML is forbidden"); }
}

function classes(el) { return String(el.className || "").split(/\s+/).filter(Boolean); }

function matches(el, selector) {
  if (selector.startsWith("#")) return el.attrs.id === selector.slice(1);
  if (selector.startsWith(".")) return classes(el).includes(selector.slice(1));
  return el.tagName === selector;
}

function findAll(root, selector) {
  const found = [];
  const walk = (node) => {
    for (const child of node.children) {
      if (child.nodeType !== 1) continue;
      if (matches(child, selector)) found.push(child);
      walk(child);
    }
  };
  walk(root);
  return found;
}

export function serialize(node) {
  if (node.nodeType === 3) return { text: node.text };
  return {
    tag: node.tagName,
    class: node.className,
    attrs: node.attrs,
    hidden: node.hidden,
    children: node.children.map(serialize),
  };
}

export function createPage() {
  const root = new Element("html");
  const make = (tag, id) => { const el = new Element(tag); if (id) el.attrs.id = id; root.append(el); return el; };
  const elements = {
    form: make("form", "chat-form"), input: make("textarea", "message"), send: make("button", "send"),
    conversation: make("div", "conversation"), count: make("span", "count"),
  };
  const welcome = new Element("div"); welcome.attrs.id = "welcome";
  elements.conversation.append(welcome);
  // The two pages and the link between them (the team page starts hidden, as in the markup).
  elements.chatPage = make("div", "chat-page");
  elements.teamPage = make("main", "team-page");
  elements.teamPage.hidden = true;
  elements.teamHeading = new Element("h1");
  elements.teamPage.append(elements.teamHeading);
  elements.teamLink = make("a", "team-link");
  const created = [];
  const document = {
    querySelector: (selector) => root.querySelector(selector),
    querySelectorAll: (selector) => root.querySelectorAll(selector),
    createElement: (tag) => { const el = new Element(tag); created.push(el); return el; },
    createElementNS: (ns, tag) => { const el = new Element(tag, ns); created.push(el); return el; },
    createTextNode: (text) => new TextNode(text),
    title: "ViharBot \u2014 A brighter way to think",
    get activeElement() { return active; },
  };
  return { root, document, elements, created };
}
