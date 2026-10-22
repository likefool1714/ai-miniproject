/* Run with Node.js: node eval/check_frontend.cjs. No browser or API key required. */
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../web_chat.html'), 'utf8').split('<script>')[1].split('</script>')[0];
function h(type, props, children) {
  if (arguments.length === 2 && (typeof props === 'string' || Array.isArray(props))) { children = props; props = null; }
  return { type, props, children };
}
const context = { Vue: { h, createApp: () => ({ mount() {} }) } };
vm.createContext(context);
vm.runInContext(source, context);
assert.equal(context.stepTone({ type: 'TaskFailed' }), 'danger');
assert.equal(context.stepTone({ type: 'ObservationFailed' }), 'danger');
assert.equal(context.stepTone({ type: 'Error' }), 'danger');
assert.equal(context.stepTone({ type: 'ModelFallback' }), 'warning');
assert.equal(context.stepTone({ type: 'Validation', verdict: '보류' }), 'warning');
assert.equal(context.stepTone({ type: 'Validation', verdict: '합격' }), 'success');
assert.equal(context.stepTone({ type: 'ClarificationRequired' }), 'neutral');
console.log('PASS: failed/held steps are not marked successful');
const numbered = context.markdownNodes('1) 현재 상태: 정상\n2) 다음 조치: 재점검')[0];
assert.equal(numbered.type, 'ol');
assert.equal(numbered.children[0].children.join(''), '현재 상태: 정상');
const mixed = context.markdownNodes('- 상태: 정상\n- 추가 확인 필요')[0];
assert.equal(mixed.children[0].children.join(''), '상태: 정상');
console.log('PASS: numbered/mixed lists preserve labels and separators');
const facts = context.markdownNodes('- 관측: CPU 55%\n- 출처: sample')[0];
assert.equal(facts.props.class, 'facts');
assert.equal(facts.children[0].children[0].children, '관측');
const table = context.markdownNodes('| 상태 | CPU |\n|---|---|\n| 정상 | 55 |')[0];
assert.equal(table.children[0].type, 'table');
console.log('PASS: observation fields and report tables render');
const unsafe = context.markdownNodes('<img src=x onerror=alert(1)> **확인** `safe`')[0];
assert.equal(unsafe.type, 'p');
assert.equal(unsafe.children[0], '<img src=x onerror=alert(1)> ');
console.log('PASS: model HTML remains text');
