import test from 'node:test'
import assert from 'node:assert/strict'
import { extractWebUrls, agentLabel, statusLabel, citationLocation } from '../src/agents.ts'

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
  assert.equal(agentLabel('public-tool'), '公共工具 Agent')
  assert.equal(statusLabel('partial'), '部分完成')
  assert.equal(statusLabel('failed'), '执行失败')
})

test('Excel citations display physical rows, legacy citations keep their location', () => {
  assert.equal(citationLocation({tool_name:'public.lookup',tool_version:2}),'public.lookup · v2')
  assert.equal(citationLocation({excel_source:{sheet_name:'参数',row_start:4,row_end:4}}),'参数 · 第 4 行')
  assert.equal(citationLocation({excel_source:{sheet_name:'参数',row_start:4,row_end:6}}),'参数 · 第 4–6 行')
  assert.equal(citationLocation({section:'正文'}),'正文')
  assert.equal(citationLocation({page_number:3}),'第 3 页')
  assert.equal(citationLocation({}),'')
  assert.equal(citationLocation({document_version:{version_number:2},excel_source:{sheet_name:'参数',row_start:4,row_end:4}}),'v2 · 参数 · 第 4 行')
})
