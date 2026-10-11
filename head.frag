// AIDA floating head: raymarched signed-distance-field robot head (face3d.py compiles this with ModernGL).
// Output is PREMULTIPLIED colour + alpha, swizzled to BGRA by face3d.py, top row first, so the GPU readback
// is exactly the Win32 DIB that UpdateLayeredWindow wants (per-pixel alpha, soft edges, glow over the desktop).
//
// Changing the look (all values are in head space, the head is about 1.9 wide):
//   shell shape ........ headSDF()          (rounded box + side pods)
//   visor (black glass)  visorSD()          (size / roundness of the face plate)
//   eyes ............... eyes()             (base size 0.105 x 0.14, centres +/-0.27, lids, iris, catch-light)
//   mouth grille ....... mouth()            (7 bars driven by the voice)
//   materials/lighting . shadeHit()         (shell colour comes from u_shell, set by face_shell in config.yaml)
//   halo / shadow ...... shade()            (outer glow strength = u_glow)
uniform float u_res;
uniform float u_time;
uniform vec3  u_rot;       // yaw, pitch, roll (radians)
uniform vec3  u_off;       // head centre offset in world units (float / bob / hop)
uniform float u_scale;     // breathing / lean
uniform vec4  u_eyeA[2];   // per eye: open 0..1, top-lid angle (rad, + = inner end down), bottom lid 0..0.5, pupil scale
uniform vec4  u_eyeB[2];   // per eye: width scale, height scale, corner roundness, brightness
uniform vec2  u_gaze;      // -1..1
uniform float u_spacing;   // extra half-distance between the eyes
uniform vec3  u_col;       // emissive colour (state colour with emotion tint)
uniform float u_glow;      // halo + emissive strength
uniform float u_bars[7];   // mouth grille bar heights 0..1
uniform vec3  u_mouth;     // curve (+ = smile), tilt (smirk), brightness
uniform float u_orbit;     // thinking orbiters 0..1
uniform float u_shadow;    // float height 0..1 (higher = smaller, lighter contact shadow)
uniform vec3  u_shell;     // shell base colour
uniform float u_pod;       // side-pod lag (secondary motion)

const float CAM_Z = 4.0;
const float FOV = 0.375;
const vec3  KEY = vec3(-0.55, 0.75, 0.62);

mat3 gR;

float sdRoundBox(vec3 p, vec3 b, float r){ vec3 q=abs(p)-b; return length(max(q,0.0))+min(max(q.x,max(q.y,q.z)),0.0)-r; }
float sdBox2(vec2 p, vec2 b, float r){ vec2 q=abs(p)-b+r; return length(max(q,0.0))+min(max(q.x,q.y),0.0)-r; }
float smin(float a,float b,float k){ float h=max(k-abs(a-b),0.0)/k; return min(a,b)-h*h*k*0.25; }
float sdCylX(vec3 p, float r, float h){ vec2 d=abs(vec2(length(p.yz),p.x))-vec2(r,h); return min(max(d.x,d.y),0.0)+length(max(d,0.0)); }

mat3 rotM(vec3 r){
  float cy=cos(r.x), sy=sin(r.x), cp=cos(r.y), sp=sin(r.y), cr=cos(r.z), sr=sin(r.z);
  mat3 Y=mat3(cy,0,-sy, 0,1,0, sy,0,cy);
  mat3 X=mat3(1,0,0, 0,cp,sp, 0,-sp,cp);
  mat3 Z=mat3(cr,sr,0, -sr,cr,0, 0,0,1);
  return Z*X*Y;
}

vec3 toHead(vec3 w){ return gR*(w-u_off)/u_scale; }

float headSDF(vec3 p){
  vec3 q = p; q.x *= 1.0 - 0.07*p.y;                       // slightly narrower chin: friendlier silhouette
  float d = sdRoundBox(q, vec3(0.44,0.26,0.28), 0.47);
  vec3 e = vec3(abs(p.x)-0.88, p.y-0.02-u_pod, p.z+0.03);  // side pods ("ears")
  float pod = sdCylX(e, 0.19, 0.06) - 0.03;
  return smin(d, pod, 0.05);
}
float map(vec3 w){ return headSDF(toHead(w))*u_scale*0.92; }
vec3 nrm(vec3 p){ vec2 e=vec2(0.0015,0.0); return normalize(vec3(map(p+e.xyy)-map(p-e.xyy), map(p+e.yxy)-map(p-e.yxy), map(p+e.yyx)-map(p-e.yyx))); }

float visorSD(vec2 q){ return sdBox2(q-vec2(0.0,0.03), vec2(0.60,0.37), 0.30); }

// studio environment for reflections: dark floor, bright top, key softbox, coloured strip behind
vec3 env(vec3 r){
  float t = r.y*0.5+0.5;
  vec3 c = mix(vec3(0.03,0.035,0.05), vec3(0.42,0.47,0.58), smoothstep(0.2,1.0,t));
  c += vec3(2.2) * smoothstep(0.86,0.97, dot(r, normalize(KEY)));
  c += vec3(0.9,0.95,1.0) * 0.8 * smoothstep(0.90,0.99, dot(r, normalize(vec3(0.75,0.35,0.55))));
  c += u_col * 1.1 * smoothstep(0.6,1.0, dot(r, normalize(vec3(0.7,-0.05,-0.7))));
  return c;
}

vec3 eyes(vec2 q, float pw){
  vec3 em = vec3(0.0);
  float bd = 10.0;
  for(int i=0;i<2;i++){
    float side = (i==0) ? -1.0 : 1.0;
    vec4 A = u_eyeA[i]; vec4 B = u_eyeB[i];
    float w = 0.122*B.x, h = 0.158*B.y;
    vec2 c = vec2(side*(0.285+u_spacing), 0.075) + u_gaze*vec2(0.035,0.03);
    vec2 e = q - c;
    float dEye = sdBox2(e, vec2(w,h), min(w,h)*B.z);
    float inner = -side;                                   // the inner end of the left eye is towards +x
    float topY = -h + 2.0*h*A.x;
    float ta = tan(A.y);
    float arch = 0.30*h*(1.0 - e.x*e.x/(w*w));          // curved lid edge, not a flat cut
    float dTop = (e.y - (topY + arch*(1.0-A.x*0.5) - ta*e.x*inner)) / sqrt(1.0+ta*ta);
    float botY = -h + 2.0*h*A.z;
    float dBot = (botY + 1.25*h*A.z*(1.0 - clamp(e.x*e.x/(w*w),0.0,1.0))) - e.y;   // arched: happy crescent
    float d = max(dEye, max(dTop, dBot));
    bd = min(bd, d);
    float m = smoothstep(pw, -pw, d);
    vec2 ic = c + u_gaze*vec2(0.028,0.022);
    float ir = min(w,h)*0.60*A.w;
    float di = length(q-ic);
    float iris = smoothstep(ir+pw, ir-pw, di);
    float ring = exp(-pow((di-ir)/0.010,2.0));
    float pup = smoothstep(ir*0.45+pw, ir*0.45-pw, di);
    float edge = smoothstep(-0.035, 0.0, d);                // brighter lens rim
    float lum = 0.62 + 0.40*iris + 0.45*ring - 0.22*pup + 0.30*edge;
    vec2 cl = (e - vec2(-0.38*w, 0.42*h)) / vec2(1.0,0.8);
    float catchl = smoothstep(0.028, 0.012, length(cl));
    vec3 c3 = u_col*lum*B.w + vec3(1.0)*(catchl*0.85 + iris*(1.0-pup)*0.22)*B.w;
    em += c3*m;
  }
  em += u_col * exp(-max(bd,0.0)*26.0) * 0.55 * u_eyeB[0].w;   // bloom
  return em;
}

vec3 mouth(vec2 q, float pw){
  float d = 10.0;
  for(int i=0;i<7;i++){
    float x = (float(i)-3.0)*0.048;
    float u = x/0.144;
    float y = -0.205 + u_mouth.x*0.04*(u*u) - u_mouth.x*0.012 + u_mouth.y*0.03*u;
    float hh = 0.008 + 0.058*u_bars[i];
    d = min(d, sdBox2(q-vec2(x,y), vec2(0.0115,hh), 0.0115));
  }
  float m = smoothstep(pw, -pw, d);
  return u_col * (m*1.15 + exp(-max(d,0.0)*40.0)*0.35) * u_mouth.z;
}

vec3 shadeHit(vec3 p, vec3 rd, float pw){
  vec3 n = nrm(p); vec3 v = -rd; vec3 hp = toHead(p); vec3 hn = gR*n;
  vec3 L = normalize(KEY);
  float ndv = max(dot(n,v),0.0);
  float fres = pow(1.0-ndv, 4.0);
  vec3 r = reflect(rd,n);
  float ao = clamp(0.45+0.55*(map(p+n*0.10)/0.10), 0.5, 1.0) * clamp(0.6+0.4*(map(p+n*0.25)/0.25),0.6,1.0);
  // shell: glossy clear-coat plastic
  float wrap = max((dot(n,L)+0.35)/1.35, 0.0);
  vec3 cool = mix(vec3(0.70,0.75,0.86), vec3(1.0), wrap);
  vec3 shell = u_shell*cool*(0.30+0.85*wrap)*ao;
  shell += env(r)*(0.05+0.65*fres)*ao;
  shell += vec3(1.0)*pow(max(dot(r,L),0.0), 90.0)*2.2 + vec3(1.0)*pow(max(dot(r,L),0.0), 12.0)*0.12;
  float back = max(dot(n, normalize(vec3(0.5,0.25,-0.8))), 0.0);
  shell += u_col * pow(1.0-ndv, 2.5) * (0.35+0.9*back) * 0.85;         // state-coloured rim light
  shell += 0.06*fres*(0.5+0.5*cos(6.2832*(fres*0.7+vec3(0.0,0.33,0.67))));   // faint iridescence
  // side-pod light rings
  vec3 pe = vec3(abs(hp.x)-0.88, hp.y-0.02-u_pod, hp.z+0.03);
  float rho = length(pe.yz);
  float outer = smoothstep(0.02, 0.05, pe.x);
  float ringd = abs(rho-0.12)-0.018;
  shell = mix(shell, shell*0.35, outer*smoothstep(0.17,0.15,rho));            // dark pod cap
  shell += u_col * outer * (smoothstep(pw,-pw,ringd)*1.4 + exp(-max(ringd,0.0)*60.0)*0.5) * u_glow;
  // visor: near-black glass with a seam
  float dv = visorSD(hp.xy);
  float vis = smoothstep(pw, -pw, dv) * smoothstep(0.0, 0.2, hn.z);
  vec3 glass = vec3(0.006,0.008,0.016) + env(r)*(0.035+0.55*fres) + vec3(1.0)*pow(max(dot(r,L),0.0),160.0)*2.0;
  glass += eyes(hp.xy, pw) + mouth(hp.xy, pw);
  vec3 col = mix(shell, glass, vis);
  col *= mix(0.3, 1.0, smoothstep(0.0, 0.016, abs(dv)) + (1.0-smoothstep(0.0,0.2,hn.z)));   // panel seam
  return col;
}

vec3 orbitPos(float a){
  vec3 c = vec3(1.13*cos(a), 0.22*sin(a)+0.05, 0.86*sin(a));
  return c + u_off;
}

vec4 shade(vec2 uv, float pw){
  vec3 ro = vec3(0.0,0.0,CAM_Z);
  vec3 rd = normalize(vec3(uv*FOV, -1.0));
  float t = 2.5, md = 10.0; bool hit = false;
  for(int i=0;i<72;i++){
    vec3 p = ro+rd*t; float d = map(p);
    md = min(md, d);
    if(d<0.0012){ hit=true; break; }
    t += d; if(t>6.2) break;
  }
  vec3 col = vec3(0.0); float a = 0.0;
  float tHit = hit ? t : 1e9;
  if(hit){
    col = shadeHit(ro+rd*t, rd, pw);
    a = 1.0;
  } else {
    float edge = 1.0 - smoothstep(0.80, 0.97, max(abs(uv.x), abs(uv.y)));
    float halo = u_glow * exp(-max(md,0.0)*8.0) * 0.42 * edge;
    // contact shadow: floats above an imaginary surface
    vec2 sc = vec2(u_off.x/1.5, -0.83);
    float sz = 1.0 - 0.25*u_shadow;
    vec2 sd = (uv - sc) / vec2(0.40*sz, 0.055*sz);
    float sh = 0.42*(1.0-0.45*u_shadow) * exp(-dot(sd,sd)*1.6) * edge;
    col = u_col*halo*1.15;
    a = 1.0 - (1.0-halo*0.85)*(1.0-sh);
  }
  if(u_orbit > 0.01){
    for(int i=0;i<3;i++) for(int k=0;k<5;k++){
      float ang = u_time*2.3 + float(i)*2.0944 - float(k)*0.11;
      vec3 c = orbitPos(ang);
      float tc = dot(c-ro, rd);
      if(tc > tHit) continue;
      float d = length(ro+rd*tc-c);
      float fk = 1.0 - float(k)*0.19;
      float rr = 0.032*fk;
      float g = (smoothstep(rr, rr*0.25, d)*1.3 + exp(-d*d/0.0035)*0.30) * fk * u_orbit;
      col += mix(u_col, vec3(1.0), 0.35*smoothstep(rr,0.0,d)) * g;
      a = max(a, min(1.0, g));
    }
  }
  col = col*(2.51*col+0.03)/(col*(2.43*col+0.59)+0.14);   // ACES-ish tonemap
  col = min(col, vec3(1.0));
  if(!hit) col = min(col, vec3(max(a, 0.0)) + col*0.0);    // keep glow premultiplied (rgb <= alpha)
  return vec4(col, a);
}

vec4 render(vec2 fc){
  gR = rotM(-u_rot);
  vec4 acc = vec4(0.0);
  float pw = 2.6/u_res;
  // 4-sample rotated grid supersampling
  vec2 o[4] = vec2[4](vec2(-0.125,-0.375), vec2(0.375,-0.125), vec2(0.125,0.375), vec2(-0.375,0.125));
  for(int s=0;s<4;s++){
    vec2 f = fc + o[s];
    vec2 uv = vec2(f.x-0.5*u_res, 0.5*u_res-f.y) / (0.5*u_res);   // y flipped: row 0 = top of image
    acc += shade(uv, pw);
  }
  return acc*0.25;
}
