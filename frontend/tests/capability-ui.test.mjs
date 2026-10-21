import test from 'node:test'
import assert from 'node:assert/strict'
import {commands,nextCommand,availabilityLabel,roleLabel,networkLabel} from '../src/capability-ui.ts'
const directory={schema_version:1,revision:3,catalog_hash:'fixture',skills:[{name:'research',description:'公开研究',version:2,available:true,availability_reason:null},{name:'admin-only',description:'管理员检查',version:1,available:false,availability_reason:'permission_required'}],tools:[{name:'public.lookup',description:'公开查询工具',available:true}]}
const capabilities={sql_enabled:false,web_read_enabled:true,web_search_enabled:false}
test('command search uses live names and descriptions and does not invent an unloaded directory',()=>{
  assert.deepEqual(commands(null,capabilities,''),[])
  assert.deepEqual(commands(directory,capabilities,'research').map(item=>item.key),['skill:research'])
  assert.deepEqual(commands(directory,capabilities,'公开 查询').map(item=>item.key),['tool:public.lookup'])
  assert.equal(commands(directory,capabilities,'admin-only')[0].disabled,true)
  assert.equal(commands(directory,capabilities,'只读数据')[0].disabled,true)
  assert.equal(commands(directory,capabilities,'public.lookup')[0].kind,'tool')
})
test('keyboard movement skips unavailable commands and wraps without selecting a disabled item',()=>{
  const items=commands(directory,capabilities,'')
  const disabled=items.findIndex(item=>item.disabled)
  assert.notEqual(nextCommand(items,disabled-1,1),disabled)
  assert.equal(nextCommand(items,items.length-1,1),0)
  assert.equal(nextCommand(items,0,-1),items.length-1)
  assert.equal(nextCommand([],0,1),-1)
  assert.equal(nextCommand([{disabled:true}],0,1),-1)
})
test('permission, approval and network labels describe actual boundaries',()=>{
  assert.equal(availabilityLabel('permission_required'),'需要相应角色')
  assert.equal(availabilityLabel('approval_not_available'),'审批尚未开放')
  assert.equal(roleLabel(['admin']),'平台管理员')
  assert.equal(networkLabel('private-index'),'授权知识库')
})
