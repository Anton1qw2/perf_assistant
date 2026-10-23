// Deliberately small subset. No HTML parsing, images, or arbitrary URL schemes.
export function safeLink(url){try{const parsed=new URL(url);return ['https:','http:'].includes(parsed.protocol)?parsed.href:null;}catch{return null;}}
export function inlineTokens(text){
  const tokens=[];const pattern=/(!?\[[^\]\n]*\]\([^\s)]+\)|\*\*[^*\n]+\*\*|`[^`\n]+`)/g;let cursor=0;
  for(const match of text.matchAll(pattern)){if(match.index>cursor)tokens.push({kind:'text',text:text.slice(cursor,match.index)});const value=match[0];
    if(value.startsWith('![')){tokens.push({kind:'text',text:'[изображение отключено]'});}
    else if(value.startsWith('[')){const end=value.indexOf('](');const href=safeLink(value.slice(end+2,-1));tokens.push(href?{kind:'link',text:value.slice(1,end),href}:{kind:'text',text:value});}
    else tokens.push({kind:value.startsWith('**')?'strong':'code',text:value.startsWith('**')?value.slice(2,-2):value.slice(1,-1)});
    cursor=match.index+value.length;
  }if(cursor<text.length)tokens.push({kind:'text',text:text.slice(cursor)});return tokens;
}
export function renderMarkdown(container,text){
  let fenced=false,code=[];
  const flush=()=>{const el=document.createElement('pre');el.textContent=code.join('\n');container.append(el);code=[];};
  for(const line of text.split('\n')){
    if(line.startsWith('```')){if(fenced)flush();fenced=!fenced;continue;}
    if(fenced){code.push(line);continue;}
    const heading=line.match(/^(#{1,3})\s+(.*)$/);const el=document.createElement(heading?'h'+(heading[1].length+2):'div');
    for(const token of inlineTokens(heading?heading[2]:line)){
      if(token.kind==='text'){el.append(document.createTextNode(token.text));continue;}
      const part=document.createElement(token.kind==='link'?'a':token.kind);part.textContent=token.text;
      if(token.href){part.href=token.href;part.target='_blank';part.rel='noopener noreferrer';}el.append(part);
    }if(!line)el.append(document.createElement('br'));container.append(el);
  }if(fenced)flush();
}
