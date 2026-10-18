import test from 'node:test'
import assert from 'node:assert/strict'
import {catalogProblem,modelSelection,profileReferences,selectedModel,supportsRole} from '../src/model-catalog.ts'

function profile(id,changes={}){return {id,version:1,display_name:id,provider:'deepseek',connection_id:'deepseek',model_name:id,context_window:32768,max_output_tokens:4096,timeout_seconds:30,enabled:true,supports_stream:true,supports_tools:true,supports_structured_output:true,fallback_profile_id:null,...changes}}
function catalog(){return {schema_version:2,profiles:{answer:profile('answer'),grader:profile('grader',{supports_stream:false,supports_tools:false})},assignments:{answer:'answer',fast:'grader',grader:'grader',evaluator:'grader'},provider_defaults:{deepseek:'answer'}}}

test('same-provider choices keep distinct profile IDs and submit no competing provider field',()=>{
  const cap={features:{model_profiles:true},default_model_profile_id:'second',models:[{profile_id:'first',provider:'deepseek',available:true},{profile_id:'second',provider:'deepseek',available:true},{profile_id:'grader',provider:'deepseek',available:false}]}
  assert.equal(selectedModel(cap,'first'),'first')
  assert.equal(selectedModel(cap,'missing'),'second')
  assert.equal(selectedModel(cap,'grader'),'second')
  assert.deepEqual(modelSelection(cap,'second'),{model_profile_id:'second'})
  const legacy={features:{model_profiles:false},default_model_provider:'ollama',models:[{provider:'ollama',available:true}]}
  assert.equal(selectedModel(legacy,'missing'),'ollama')
  assert.deepEqual(modelSelection(legacy,'ollama'),{model_provider:'ollama'})
  assert.equal(selectedModel({...cap,models:[]},'first'),'')
})

test('role capabilities and references prevent disabling or deleting assigned models',()=>{
  const data=catalog()
  assert.equal(catalogProblem(data),'')
  assert.equal(supportsRole(data.profiles.grader,'answer'),false)
  assert.equal(supportsRole(data.profiles.grader,'evaluator'),true)
  assert.deepEqual(profileReferences(data,'answer'),['回答与工具调度','deepseek 默认模型'])
  data.profiles.answer.enabled=false
  assert.match(catalogProblem(data),/回答与工具调度/)
  data.profiles.answer.enabled=true
  data.assignments.evaluator='missing'
  assert.match(catalogProblem(data),/质量评测/)
})

test('fallback cannot chain or lose role capabilities and model output reserves input budget',()=>{
  const data=catalog()
  data.profiles.backup=profile('backup')
  data.profiles.answer.fallback_profile_id='backup'
  assert.equal(catalogProblem(data),'')
  assert.deepEqual(profileReferences(data,'backup'),['answer 的降级模型'])
  data.profiles.backup.supports_tools=false
  assert.match(catalogProblem(data),/能力不匹配/)
  data.profiles.backup.supports_tools=true
  data.profiles.backup.fallback_profile_id='answer'
  assert.match(catalogProblem(data),/最多允许一次降级/)
  data.profiles.backup.fallback_profile_id=null
  data.profiles.grader.context_window=4096
  assert.match(catalogProblem(data),/1024/)
})
