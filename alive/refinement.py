'''
Refinement checking for optimizations.
'''

from . import config
from . import error
from . import smtinterp
import z3
import glob
import logging
import time
from .language import *
from .z3util import mk_and, mk_not, mk_forall

logger = logging.getLogger(__name__)

PRESAFE, TGTSAFE, UB, POISON, UNEQUAL = range(5)

def check(opt, type_model, encoding=config.encoding):
  logger.info('Checking refinement of %r', opt.name)

  encoding = smtinterp.lookup(encoding)
  smt = encoding(type_model)

  asm = smt.conjunction(opt.asm)
  premise = asm.aux + asm.safe + asm.value
  if asm.defined or asm.nonpoison:
    raise Exception('Defined/Non-poison condition declared by assumption')

  pre = smt.conjunction(opt.pre)
  premise += pre.aux
  if pre.defined or pre.nonpoison:
    raise Exception('Defined/Non-poison condition declared by precondition')


  src = smt(opt.src)
  if src.aux:
    raise Exception('Auxiliary condition declared by source')

  tgt = smt(opt.tgt)
  premise += tgt.aux

  def err(c, m):
    return CounterExampleError(
      c, m, type_model, opt.src, src.value, tgt.value, encoding)

  if pre.safe:
    check_expr(PRESAFE, mk_and(premise + [mk_not(pre.safe)]), opt, err)

  premise += pre.value

  if tgt.safe:
    check_expr(TGTSAFE, mk_and(premise + [mk_not(tgt.safe)]), opt, err)

  premise += src.defined
  if config.poison_undef:
    premise += src.nonpoison

  if tgt.defined:
    expr = premise + [mk_not(tgt.defined)]
    check_expr(UB, mk_forall(src.qvars, expr), opt, err)

  if not config.poison_undef:
    premise += src.nonpoison

  if tgt.nonpoison:
    check_expr(POISON,
      mk_forall(src.qvars, premise + [mk_not(tgt.nonpoison)]), opt, err)

  check_expr(UNEQUAL,
    mk_forall(src.qvars, premise + [z3.Not(src.value == tgt.value)]), opt, err)

_stage_name = {
  PRESAFE: 'precondition safety',
  TGTSAFE: 'target safety',
  UB:      'undefined behavior',
  POISON:  'poison',
  UNEQUAL: 'equality',
}

header = '''(set-info :source |
 Generated by Alive-NJ
 More info in N. P. Lopes, D. Menendez, S. Nagarakatte, J. Regehr.
 Provably Correct Peephole Optimizations with Alive. In PLDI'15.
|)
'''

def check_expr(stage, expr, opt, err):
  s = z3.Solver()
  if config.timeout is not None:
    s.set('timeout', config.timeout)
  s.add(expr)
  logger.info('%s check\n%s', _stage_name[stage], s)

  time0 = time.time()
  res = s.check()
  time1 = time.time()

  solve_time = time1 - time0

  if logger.isEnabledFor(logging.INFO):
    logger.info('\nresult: %s\ntime: %s\nstats:\n%s', res, solve_time,
      s.statistics())

  if config.bench_dir and solve_time >= config.bench_threshold:
    files = glob.glob(config.bench_dir + '/*.smt2')
    filename = '{0}/{1:03d}.smt2'.format(config.bench_dir, len(files))
    fd = open(filename, 'w')
    fd.write(header)
    fd.write('; {0} check for {1!r}\n'.format(_stage_name[stage], opt.name))
    fd.write('; time: {0} s\n\n'.format(solve_time))
    fd.write(s.to_smt2())
    fd.close()

  if res == z3.sat:
    m = s.model()
    logger.info('counterexample: %s', m)

    raise err(stage, m)

  if res == z3.unknown:
    raise Error('Model returned unknown: ' + s.reason_unknown())

  return None



def format_z3val(val):
  if isinstance(val, z3.BitVecNumRef):
    w = val.size()
    u = val.as_long()
    s = val.as_signed_long()

    if u == s:
      return '0x{1:0{0}x} ({1})'.format((w+3)/4, u)
    return '0x{1:0{0}x} ({1}, {2})'.format((w+3)/4, u, s)

  if isinstance(val, z3.FPRef):
    return str(val)

class Error(error.Error):
  pass

class CounterExampleError(Error):
  def __init__(self, cause, model, types, src, srcv, tgtv, trans):
    self.cause = cause
    self.model = model
    self.types = types
    self.src   = src
    self.srcv  = srcv
    self.tgtv  = tgtv
    self.trans = trans

  cause_str = {
    PRESAFE: 'Precondition is unsafe',
    TGTSAFE: 'Target is unsafe',
    UB:      'Target introduces undefined behavior',
    POISON:  'Target introduces poison',
    UNEQUAL: 'Mismatch in values',
    }

  def __str__(self):

    smt = self.trans(self.types)

    vars = [v for v in proper_subterms(self.src)
              if isinstance(v, (Input, Instruction))]

    ty_width = 1
    name_width = 1
    rows = []
    for v in vars:
      ty = str(self.types[v])
      ty_width = max(ty_width, len(ty))

      name = v.name
      name_width = max(name_width, len(name))

      interp = smt(v)

      if z3.is_false(self.model.evaluate(mk_and(interp.nonpoison))):
        # FIXME: make sure interp.nonpoison fully evaluates
        # e.g., what if it depends on a qvar somehow?
        rows.append((ty, name, 'poison'))

      else:
        val = self.model.evaluate(smt.eval(v), model_completion=True)
        # this will pick arbitrary values for any source qvars or
        # other unconstrained values

        rows.append((ty, name, format_z3val(val)))

    interp = smt(self.src)
    if z3.is_false(self.model.evaluate(mk_and(interp.nonpoison))):
      srcval = 'poison'
    else:
      srcval = format_z3val(self.model.evaluate(self.srcv, True))

    if self.cause == UB:
      tgtval = 'undefined'
    elif self.cause == POISON:
      tgtval = 'poison'
    else:
      tgtval = format_z3val(self.model.evaluate(self.tgtv, True))

    return '''{cause} for {srcty} {src}

Example:
{table}
source: {srcval}
target: {tgtval}'''.format(
      cause = self.cause_str[self.cause],
      srcty = self.types[self.src],
      src = self.src.name,
      table = '\n'.join(
        '{0:>{1}} {2:{3}} = {4}'.format(ty, ty_width, name, name_width, val)
        for ty, name, val in rows),
      srcval = srcval,
      tgtval = tgtval,
    )
