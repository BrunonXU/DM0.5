// Existing upright joint coupling, now bounded by the active robot contract.
// This is an angle-space path, not a calibrated Cartesian height axis.
export const liftNames=['body_j3','body_j4','body_j5'];
const ratios=[1,2,1];
export function liftRange(limits){
 let lower=-Infinity,upper=Infinity,limitingJoint=null;
 liftNames.forEach((name,i)=>{
  const bounds=limits?.[name];
  if(!Array.isArray(bounds)||bounds.length!==2||!bounds.every(Number.isFinite)||bounds[0]>=bounds[1])throw Error('缺少 F1 腰部关节限位');
  lower=Math.max(lower,-bounds[1]/ratios[i]);
  const end=-bounds[0]/ratios[i];if(end<upper){upper=end;limitingJoint=name;}
 });
 if(!(lower<upper))throw Error('腰部联动范围无交集');
 return {lower,upper,limitingJoint,low:ratios.map(r=>-r*lower),high:ratios.map(r=>-r*upper)};
}
export function liftTarget(percent,limits){
 if(!Number.isFinite(percent)||percent<0||percent>100)throw Error('升降目标必须在 0–100%');
 const range=liftRange(limits),scalar=range.lower+(range.upper-range.lower)*percent/100;
 return Object.fromEntries(liftNames.map((name,i)=>[name,Math.round(-ratios[i]*scalar*1000)/1000]));
}
export function projectLift(positions,limits){
 const range=liftRange(limits);
 if(!positions.every(Number.isFinite)||positions.length!==3)throw Error('腰部实测值缺失');
 const scalar=-positions.reduce((sum,value,i)=>sum+value*ratios[i],0)/ratios.reduce((sum,r)=>sum+r*r,0);
 return Math.max(0,Math.min(100,(scalar-range.lower)/(range.upper-range.lower)*100));
}
export function presetPercent(level,limits){
 const range=liftRange(limits),scalar=level*5;
 if(!Number.isInteger(level)||level<1||level>4||scalar<range.lower||scalar>range.upper)throw Error('快捷姿态超出当前 F1 联动范围');
 return (scalar-range.lower)/(range.upper-range.lower)*100;
}
