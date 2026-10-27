import type {CapabilityDirectory,Capabilities} from './api'
export const modeLabels={auto:'自动调度',knowledge:'知识库',web:'公开网络',sql:'只读数据'}
export function availabilityLabel(reason:string|null){return ({disabled:'已停用',permission_required:'需要相应角色',not_configured:'尚未配置',approval_not_available:'审批尚未开放',deleted:'已删除'} as Record<string,string>)[reason||'']||'可使用'}
export function roleLabel(roles:string[]){return roles.length?roles.map(role=>role==='admin'?'平台管理员':'普通用户').join(' / '):'所有已登录账号'}
export function networkLabel(policy:string){return ({'private-index':'授权知识库','configured-database':'指定数据库','public-web':'公开网络','public-https':'公开 HTTPS',none:'无网络'} as Record<string,string>)[policy]||policy}
export type Command={key:string;kind:'mode'|'skill'|'tool';name:string;label:string;description:string;disabled:boolean;reason:string}
export function commands(directory:CapabilityDirectory|null,capabilities:Capabilities|null,query:string):Command[]{
  if(!directory)return []
  const items:Command[]=Object.entries(modeLabels).map(([name,label])=>({key:`mode:${name}`,kind:'mode' as const,name,label,
    description:'切换当前对话的问答模式',disabled:name==='sql'&&!capabilities?.sql_enabled||name==='web'&&!capabilities?.web_read_enabled&&!capabilities?.web_search_enabled,reason:'尚未配置'}))
  items.push(...directory.skills.map(item=>({key:`skill:${item.name}`,kind:'skill' as const,name:item.name,label:item.description,
    description:`/${item.name} · v${item.version}`,disabled:!item.available,reason:availabilityLabel(item.availability_reason)})))
  items.push(...directory.tools.map(item=>({key:`tool:${item.name}`,kind:'tool' as const,name:item.name,label:item.name,
    description:`查看工具 · ${item.description}`,disabled:false,reason:''})))
  const words=query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean)
  return items.filter(item=>words.every(word=>`${item.label} ${item.description} ${item.name}`.toLocaleLowerCase().includes(word)))
}
export function nextCommand(items:Command[],start:number,direction:number){
  for(let step=1;step<=items.length;step++){const index=(start+direction*step+items.length*2)%items.length;if(!items[index]?.disabled)return index}
  return -1
}

export function resourceLabel(scope:string){return ({'authorized-knowledge':'当前授权资料','readonly-allowlist':'只读表白名单','public-query':'本轮公开输入','public-web':'本轮公开输入','isolated-workspace':'本次临时工作区',none:'无额外资源'} as Record<string,string>)[scope]||'工具声明的资源范围'}
