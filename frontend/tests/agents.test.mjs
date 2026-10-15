import test from 'node:test'
import assert from 'node:assert/strict'
import { extractWebUrls, agentLabel, statusLabel } from '../src/agents.ts'

test('extract only user message URLs, strip prose punctuation and deduplicate', () => {
  assert.deepEqual(extractWebUrls('阅读 https://example.com/a，和（https://other.example/b）。https://example.com/a'),
    ['https://example.com/a', 'https://other.example/b'])
  assert.deepEqual(extractWebUrls('参考 [链接](https://example.com/a?q=1)。'), ['https://example.com/a?q=1'])
  assert.deepEqual(extractWebUrls('没有网页'), [])
})

test('recognize more than three URLs so the composer can reject instead of silently dropping', () => {
  assert.equal(extractWebUrls('https://a.example https://b.example https://c.example https://d.example').length, 4)
})

test('agent activity has readable labels', () => {
  assert.equal(agentLabel('sql'), 'SQL Agent')
  assert.equal(statusLabel('partial'), '部分完成')
  assert.equal(statusLabel('failed'), '执行失败')
})
