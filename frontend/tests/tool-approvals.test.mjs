import test from 'node:test'
import assert from 'node:assert/strict'
import {requestedSkill,toolApprovalDraft,sandboxBudget} from '../src/tool-approvals.ts'
const skill={name:'public-work',available:true,approval_tools:['public.write']}
const tool={name:'public.write',available:true,requires_approval:true,version:4}
const directory={revision:8,skills:[skill],tools:[tool]}
const limits={timeout_seconds:15,memory_bytes:134217728,cpu_count:1,pids_limit:32,output_bytes:65536,workspace_bytes:16777216,max_files:8,max_file_bytes:1048576,max_artifact_bytes:2097152}

test('isolated execution displays the exact frozen time, memory and export budgets',()=>{
  const sandbox={...tool,resource_scope:'isolated-workspace',sandbox_limits:limits}
  assert.deepEqual(sandboxBudget(sandbox),[['执行时间','15 秒'],['内存','128 MiB'],['CPU','1 核'],['进程数','32'],['临时空间','16 MiB'],['输出','64 KiB'],['生成文件','最多 8 份 · 单份 1 MiB · 合计 2 MiB']])
  assert.deepEqual(sandboxBudget({...tool,name:'sandbox_execute',resource_scope:'public-web'}),[])
  for(const broken of [undefined,{...limits,cpu_count:NaN},{...limits,max_files:1.5},{...limits,max_artifact_bytes:1}]){
    assert.throws(()=>toolApprovalDraft({...directory,tools:[{...sandbox,sandbox_limits:broken}]},'admin',true,skill.name),/资源限额不完整/)
  }
})

test('explicit selection and slash command agree while remember remains a memory command',()=>{
  assert.equal(requestedSkill('/public-work question',null),'public-work')
  assert.equal(requestedSkill('question','public-work'),'public-work')
  assert.equal(requestedSkill('/public-work question','public-work'),'public-work')
  assert.equal(requestedSkill('/remember preference',null),null)
  assert.throws(()=>requestedSkill('/other question','public-work'),/不一致/)
})

test('approval draft binds the displayed revision and actual tool versions',()=>{
  const draft=toolApprovalDraft(directory,'admin',true,'public-work')
  assert.deepEqual(draft,{skillName:'public-work',revision:8,tools:[tool]})
  assert.equal(toolApprovalDraft(directory,'admin',true,null),null)
  assert.equal(toolApprovalDraft({...directory,skills:[{...skill,approval_tools:[]}]},'user',true,'public-work'),null)
})

test('disabled, incomplete, unconfigured and non-admin approval cannot produce a draft',()=>{
  assert.throws(()=>toolApprovalDraft(null,'admin',true,'public-work'),/重新选择/)
  assert.throws(()=>toolApprovalDraft(directory,'user',true,'public-work'),/管理员/)
  assert.throws(()=>toolApprovalDraft(directory,'admin',false,'public-work'),/尚未开放/)
  assert.throws(()=>toolApprovalDraft({...directory,tools:[]},'admin',true,'public-work'),/状态已变化/)
  assert.throws(()=>toolApprovalDraft({...directory,skills:[{...skill,available:false}]},'admin',true,'public-work'),/重新选择/)
  assert.throws(()=>toolApprovalDraft({...directory,tools:[{...tool,requires_approval:false}]},'admin',true,'public-work'),/状态已变化/)
})
