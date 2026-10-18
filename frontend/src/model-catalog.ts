import type {Capabilities,ModelCatalog,ModelRole,RegisteredProfile} from './api'

export function modelSelection(capabilities:Capabilities|null,selection:string){
  return capabilities?.features.model_profiles?{model_profile_id:selection}:{model_provider:selection}
}
export function selectedModel(capabilities:Capabilities,selection:string){
  const key=(model:Capabilities['models'][number])=>capabilities.features.model_profiles?model.profile_id||model.provider:model.provider
  const available=capabilities.models.filter(model=>model.available)
  if(available.some(model=>key(model)===selection))return selection
  const preferred=capabilities.features.model_profiles?capabilities.default_model_profile_id:capabilities.default_model_provider
  return available.find(model=>key(model)===preferred)?preferred!:available[0]?key(available[0]):''
}

export const modelRoles:ModelRole[]=['answer','fast','grader','evaluator']
export const roleLabels:Record<ModelRole,string>={answer:'回答与工具调度',fast:'快速规划与改写',grader:'证据评判',evaluator:'质量评测'}
export function supportsRole(profile:RegisteredProfile|undefined,role:ModelRole):boolean{
  return !!profile?.enabled&&(role==='answer'?profile.supports_stream&&profile.supports_tools:profile.supports_structured_output)
}
export function profileReferences(catalog:ModelCatalog,id:string):string[]{
  return [...modelRoles.filter(role=>catalog.assignments[role]===id).map(role=>roleLabels[role]),
    ...Object.entries(catalog.provider_defaults).filter(([,value])=>value===id).map(([provider])=>`${provider} 默认模型`),
    ...Object.values(catalog.profiles).filter(profile=>profile.fallback_profile_id===id).map(profile=>`${profile.display_name} 的降级模型`)]
}
export function catalogProblem(catalog:ModelCatalog):string{
  for(const role of modelRoles)if(!supportsRole(catalog.profiles[catalog.assignments[role]],role))return `请为“${roleLabels[role]}”选择已启用且能力匹配的模型。`
  for(const [provider,id] of Object.entries(catalog.provider_defaults))if(!supportsRole(catalog.profiles[id], 'answer')||catalog.profiles[id]?.provider!==provider)return `${provider} 默认模型不具备回答能力。`
  for(const profile of Object.values(catalog.profiles)){
    if(profile.context_window<profile.max_output_tokens+1024)return `${profile.display_name} 至少需要预留 1024 输入 Token。`
    if(profile.fallback_profile_id){
      const fallback=catalog.profiles[profile.fallback_profile_id]
      if(!fallback?.enabled||fallback.id===profile.id||fallback.fallback_profile_id)return `${profile.display_name} 的降级配置无效，最多允许一次降级。`
      const roles=modelRoles.filter(role=>catalog.assignments[role]===profile.id)
      if(Object.values(catalog.provider_defaults).includes(profile.id))roles.push('answer')
      if(roles.some(role=>!supportsRole(fallback,role)))return `${profile.display_name} 的降级模型能力不匹配。`
    }
  }
  return ''
}
