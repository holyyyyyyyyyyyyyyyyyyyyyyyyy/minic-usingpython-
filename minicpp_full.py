#!/usr/bin/env python3
import os
import re
import subprocess
import sys
import tempfile

sys.setrecursionlimit(10000)

ALLOWED_INCLUDES = frozenset(('iostream', 'stdio.h'))
_INCLUDE_RE = re.compile(r'#include\s*[<"]([^>"]+)[>"]')
_USING_STD_RE = re.compile(r'^using\s+namespace\s+std\s*;\s*$')

def preprocess(src):
    out = []
    append = out.append
    for lineno, line in enumerate(src.splitlines(), 1):
        s = line.strip()
        if s.startswith('#include'):
            m = _INCLUDE_RE.fullmatch(s)
            if not m:
                raise SyntaxError(f'line {lineno}: malformed #include')
            name = m.group(1)
            if name not in ALLOWED_INCLUDES:
                raise SyntaxError(f'line {lineno}: only <iostream> and <stdio.h> are supported')
            append('')
        elif _USING_STD_RE.match(s):
            append('')
        else:
            append(line)
    return '\n'.join(out)

class Tok:
    __slots__ = ('kind', 'val', 'line', 'col')
    def __init__(self, kind, val, line, col):
        self.kind = kind
        self.val = val
        self.line = line
        self.col = col
    def __repr__(self):
        return f'Tok({self.kind},{self.val!r},{self.line}:{self.col})'

_TOKEN_SPEC = (
    ('ws',      r'[ \t\r\n]+'),
    ('comment', r'//[^\n]*|/\*[\s\S]*?\*/'),
    ('string',  r'"(?:\\.|[^"\\])*"'),
    ('charlit', r"'(?:\\.|[^'\\])*'"),
    ('num',     r'(?:\d+\.\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?|\d+[eE][+-]?\d+|\d+)[fFuUlL]*'),
    ('id',      r'[A-Za-z_]\w*'),
    ('op',      r'>>=|<<=|->\*|\+\+|--|->|<<|>>|<=|>=|==|!=|&&|\|\||\+=|-=|\*=|/=|%=|&=|\|=|\^=|::|\?\?|[-+*/%<>=!&|^~?:;,.(){}\[\]]'),
)
_MASTER = re.compile('|'.join(f'(?P<{n}>{p})' for n, p in _TOKEN_SPEC))

def tokenize(src):
    toks = []
    append = toks.append
    Tok_ = Tok
    line = 1
    col = 1
    prev_end = 0
    src_len = len(src)
    for m in _MASTER.finditer(src):
        start = m.start()
        if start != prev_end:
            raise SyntaxError(f'line {line}: unexpected character {src[prev_end]!r}')
        kind = m.lastgroup
        text = m.group()
        if kind == 'ws' or kind == 'comment':
            nl = text.rfind('\n')
            if nl >= 0:
                line += text.count('\n')
                col = len(text) - nl
            else:
                col += len(text)
        else:
            append(Tok_(kind, text, line, col))
            col += len(text)
        prev_end = m.end()
    if prev_end != src_len:
        raise SyntaxError(f'line {line}: unexpected character {src[prev_end]!r}')
    append(Tok_('eof', '', line, col))
    return toks

TYPE_WORDS = frozenset(('void', 'bool', 'char', 'short', 'int', 'long',
                        'float', 'double', 'signed', 'unsigned', 'const', 'auto'))
STORAGE_WORDS = frozenset(('static', 'extern', 'register', 'volatile', 'inline'))

class Parser:
    __slots__ = ('t', 'i', 'funcs', 'globals', 'enums', 'scopes', 'cout_mode')

    def __init__(self, toks):
        self.t = toks
        self.i = 0
        self.funcs = {}
        self.globals = {}
        self.enums = {}
        self.scopes = [{}]
        self.cout_mode = False

    def peek(self, k=0):
        j = self.i + k
        t = self.t
        return t[j] if j < len(t) else t[-1]

    def val(self, k=0):
        j = self.i + k
        t = self.t
        return t[j].val if j < len(t) else ''

    def kind(self, k=0):
        j = self.i + k
        t = self.t
        return t[j].kind if j < len(t) else 'eof'

    def at(self, v):
        return self.t[self.i].val == v

    def eof(self):
        return self.t[self.i].kind == 'eof'

    def adv(self):
        tok = self.t[self.i]
        if self.i < len(self.t) - 1:
            self.i += 1
        return tok

    def eat(self, v):
        if self.t[self.i].val == v:
            return self.adv()
        return None

    def expect(self, v):
        tok = self.t[self.i]
        if tok.val != v:
            raise SyntaxError(f'line {tok.line}: expected {v!r}, found {tok.val!r}')
        return self.adv()

    def expect_id(self):
        tok = self.t[self.i]
        if tok.kind != 'id':
            raise SyntaxError(f'line {tok.line}: expected identifier, found {tok.val!r}')
        return self.adv().val

    def is_kw(self, name):
        v = self.t[self.i].val
        return v == name or (v == 'std' and self.val(1) == '::' and self.val(2) == name)

    def eat_kw(self, name):
        v = self.t[self.i].val
        if v == name:
            self.adv()
            return True
        if v == 'std' and self.val(1) == '::' and self.val(2) == name:
            self.adv(); self.adv(); self.adv()
            return True
        return False

    def lookup(self, name):
        for s in reversed(self.scopes):
            if name in s:
                return s[name]
        if name in self.globals:
            return self.globals[name]
        if name in self.enums:
            return 'int'
        return None

    def at_type(self):
        v = self.t[self.i].val
        return v in TYPE_WORDS or v == 'enum'

    def parse_type(self):
        const = False
        signedness = None
        base = None
        v = self.t[self.i].val

        while v in ('const', 'signed', 'unsigned'):
            self.adv()
            if v == 'const':
                const = True
            else:
                signedness = v
            v = self.t[self.i].val

        if v == 'auto':
            self.adv(); base = 'auto'
        elif v == 'long':
            self.adv()
            if self.eat('long'):
                base = 'long long'
            else:
                base = 'long'
            self.eat('int')
        elif v == 'short':
            self.adv(); base = 'short'
            self.eat('int')
        elif v == 'int':
            self.adv(); base = 'int'
        elif v == 'char':
            self.adv(); base = 'char'
        elif v == 'bool':
            self.adv(); base = 'bool'
        elif v == 'float':
            self.adv(); base = 'float'
        elif v == 'double':
            self.adv(); base = 'double'
        elif v == 'void':
            self.adv(); base = 'void'
        else:
            return None

        if signedness and base == 'char':
            base = 'signed char' if signedness == 'signed' else 'unsigned char'
        elif signedness:
            base = f'{signedness} {base}'

        t = base
        while self.eat('*'):
            t += '*'
        while self.eat('&'):
            t += '&'
        return ('const ' if const else '') + t

    def is_storage(self):
        return self.t[self.i].val in STORAGE_WORDS

    def parse_decl_spec(self):
        storage = []
        while self.t[self.i].val in STORAGE_WORDS:
            storage.append(self.adv().val)
        return storage, self.parse_type()

    def parse_program(self):
        items = []
        append = items.append
        while not self.eof():
            if self.eat(';'):
                continue
            if self.is_kw('using'):
                self.parse_using()
                continue
            if self.at('enum'):
                append(self.parse_enum())
                continue
            storage, ty = self.parse_decl_spec()
            if ty is None:
                tok = self.t[self.i]
                raise SyntaxError(f'line {tok.line}: expected declaration, found {tok.val!r}')
            if self.at('('):
                raise SyntaxError(f'line {self.t[self.i].line}: missing function name')
            name = self.parse_declarator_name()
            if self.at('('):
                params = self.parse_params()
                if self.eat(';'):
                    defined = False
                    body = None
                else:
                    body = self.parse_block()
                    defined = True
                self.funcs[name] = {'ret': ty, 'params': params, 'defined': defined}
                append(('func', ty, name, params, body, storage, defined))
                continue
            items.extend(self.parse_global_decl_rest(storage, ty, name))
        return items

    def parse_using(self):
        self.expect('using')
        if self.eat('namespace'):
            if self.t[self.i].val == 'std':
                self.adv()
            else:
                self.expect_id()
            self.expect(';')
            return
        while not self.eof() and not self.eat(';'):
            self.adv()

    def parse_enum(self):
        self.expect('enum')
        scoped = False
        if self.at('class') or self.at('struct'):
            scoped = True
            self.adv()
        enum_name = None
        if self.kind() == 'id' and not self.at('{'):
            enum_name = self.adv().val
        self.expect('{')
        values = []
        next_val = 0
        while not self.at('}'):
            name = self.expect_id()
            if self.eat('='):
                expr = self.parse_expr()
            else:
                expr = ('num', str(next_val))
            values.append((name, expr))
            self.enums[name] = 'int'
            next_val += 1
            if not self.eat(','):
                break
        self.expect('}')
        self.eat(';')
        return ('enum', enum_name, values, scoped)

    def parse_declarator_name(self):
        while self.at('*') or self.at('&'):
            self.adv()
        return self.expect_id()

    def parse_global_decl_rest(self, storage, ty, first_name):
        items = []
        name = first_name
        while True:
            dims = self.parse_array_dims()
            init = self.parse_initializer() if self.eat('=') else None
            items.append(('gvar', ty, name, dims, init, storage))
            self.globals[name] = ty + ('[]' if dims else '')
            if not self.eat(','):
                break
            name = self.parse_declarator_name()
        self.expect(';')
        return items

    def parse_params(self):
        self.expect('(')
        params = []
        if self.at('void') and self.val(1) == ')':
            self.adv()
        elif not self.at(')'):
            while True:
                storage, ty = self.parse_decl_spec()
                if ty is None:
                    raise SyntaxError(f'line {self.t[self.i].line}: expected parameter type')
                if self.kind() == 'id':
                    name = self.adv().val
                else:
                    name = f'_p{len(params)}'
                if self.parse_array_dims():
                    ty += '*'
                params.append((ty, name, storage))
                if not self.eat(','):
                    break
        self.expect(')')
        return params

    def parse_block(self):
        self.expect('{')
        self.scopes.append({})
        out = []
        append = out.append
        while not self.eof() and not self.at('}'):
            append(self.parse_stmt())
        if self.eof():
            raise SyntaxError('unexpected end of file inside block')
        self.expect('}')
        self.scopes.pop()
        return ('block', out)

    def parse_array_dims(self):
        dims = []
        while self.eat('['):
            if self.at(']'):
                dims.append(None)
            else:
                dims.append(self.parse_expr())
            self.expect(']')
        return dims

    def parse_initializer(self):
        if self.eat('{'):
            vals = []
            while not self.at('}'):
                vals.append(self.parse_initializer())
                if not self.eat(','):
                    break
            self.expect('}')
            return ('initlist', vals)
        return self.parse_assign()

    def parse_decl(self, require_semicolon=True):
        storage, ty = self.parse_decl_spec()
        if ty is None:
            raise SyntaxError(f'line {self.t[self.i].line}: expected type')
        items = []
        while True:
            name = self.parse_declarator_name()
            dims = self.parse_array_dims()
            init = self.parse_initializer() if self.eat('=') else None
            if ty == 'auto':
                ty = self.infer(init) if init is not None else 'int'
            actual = ty + ('[]' if dims else '')
            self.scopes[-1][name] = actual
            items.append((name, init, actual, dims, storage))
            if not self.eat(','):
                break
        if require_semicolon:
            self.expect(';')
        return ('decl', ty, items)

    def parse_for(self):
        self.expect('for'); self.expect('(')
        self.scopes.append({})
        if self.at(';'):
            self.adv(); init = None
        elif self.at_type() or self.is_storage():
            init = self.parse_decl()
        else:
            init = ('expr', self.parse_expr())
            self.expect(';')
        cond = None if self.at(';') else self.parse_expr()
        self.expect(';')
        step = None if self.at(')') else self.parse_expr()
        self.expect(')')
        body = self.parse_stmt()
        self.scopes.pop()
        return ('for', init, cond, step, body)

    def parse_switch(self):
        self.expect('switch'); self.expect('(')
        e = self.parse_expr(); self.expect(')'); self.expect('{')
        groups = []
        current = None
        while not self.eof() and not self.at('}'):
            if self.eat('case'):
                expr = self.parse_expr(); self.expect(':')
                current = ('case', expr, [])
                groups.append(current)
                continue
            if self.eat('default'):
                self.expect(':')
                current = ('default', None, [])
                groups.append(current)
                continue
            if current is None:
                raise SyntaxError(f'line {self.t[self.i].line}: statement before case/default in switch')
            current[2].append(self.parse_stmt())
        self.expect('}')
        return ('switch', e, groups)

    def parse_stmt(self):
        v = self.t[self.i].val
        if v == '{':
            return self.parse_block()
        if self.eat(';'):
            return ('empty',)
        if v == 'if':
            self.adv(); self.expect('(')
            c = self.parse_expr(); self.expect(')')
            a = self.parse_stmt()
            b = self.parse_stmt() if self.eat('else') else None
            return ('if', c, a, b)
        if v == 'while':
            self.adv(); self.expect('(')
            c = self.parse_expr(); self.expect(')')
            return ('while', c, self.parse_stmt())
        if v == 'do':
            self.adv()
            body = self.parse_stmt()
            self.expect('while'); self.expect('(')
            c = self.parse_expr(); self.expect(')'); self.expect(';')
            return ('do', body, c)
        if v == 'for':
            return self.parse_for()
        if v == 'switch':
            return self.parse_switch()
        if v == 'return':
            self.adv()
            e = None if self.at(';') else self.parse_expr()
            self.expect(';')
            return ('return', e)
        if v == 'break':
            self.adv(); self.expect(';'); return ('break',)
        if v == 'continue':
            self.adv(); self.expect(';'); return ('continue',)
        if v in TYPE_WORDS or v in STORAGE_WORDS or v == 'enum':
            return self.parse_decl()
        if self.is_kw('cout'):
            return self.parse_cout()
        if self.is_kw('cin'):
            return self.parse_cin()
        e = self.parse_expr(); self.expect(';')
        return ('expr', e)

    def parse_cout(self):
        self.eat_kw('cout')
        items = []
        while self.eat('<<'):
            if self.eat_kw('endl'):
                items.append(('endl',))
            else:
                old = self.cout_mode
                self.cout_mode = True
                items.append(self.parse_expr())
                self.cout_mode = old
        self.expect(';')
        return ('cout', items)

    def parse_cin(self):
        self.eat_kw('cin')
        items = []
        while self.eat('>>'):
            items.append(self.parse_unary())
        self.expect(';')
        return ('cin', items)

    def parse_expr(self):
        e = self.parse_assign()
        while self.eat(','):
            e = ('comma', e, self.parse_assign())
        return e

    def parse_assign(self):
        left = self.parse_cond()
        v = self.t[self.i].val
        if v in ('=', '+=', '-=', '*=', '/=', '%=', '&=', '|=', '^=', '<<=', '>>='):
            self.adv()
            return ('assign', v, left, self.parse_assign())
        return left

    def parse_cond(self):
        e = self.parse_lor()
        if self.eat('?'):
            a = self.parse_expr()
            self.expect(':')
            return ('cond', e, a, self.parse_cond())
        return e

    def parse_lor(self):
        e = self.parse_land()
        while self.eat('||'):
            e = ('binop', '||', e, self.parse_land())
        return e

    def parse_land(self):
        e = self.parse_bitor()
        while self.eat('&&'):
            e = ('binop', '&&', e, self.parse_bitor())
        return e

    def parse_bitor(self):
        e = self.parse_bitxor()
        while self.eat('|'):
            e = ('binop', '|', e, self.parse_bitxor())
        return e

    def parse_bitxor(self):
        e = self.parse_bitand()
        while self.eat('^'):
            e = ('binop', '^', e, self.parse_bitand())
        return e

    def parse_bitand(self):
        e = self.parse_eq()
        while self.eat('&'):
            e = ('binop', '&', e, self.parse_eq())
        return e

    def parse_eq(self):
        e = self.parse_rel()
        while True:
            v = self.t[self.i].val
            if v == '==' or v == '!=':
                self.adv()
                e = ('binop', v, e, self.parse_rel())
            else:
                return e

    def parse_rel(self):
        e = self.parse_shift()
        while True:
            v = self.t[self.i].val
            if v in ('<', '>', '<=', '>='):
                self.adv()
                e = ('binop', v, e, self.parse_shift())
            else:
                return e

    def parse_shift(self):
        e = self.parse_add()
        while True:
            v = self.t[self.i].val
            if v in ('<<', '>>') and not (self.cout_mode and v == '<<'):
                self.adv()
                e = ('binop', v, e, self.parse_add())
            else:
                return e

    def parse_add(self):
        e = self.parse_mul()
        while True:
            v = self.t[self.i].val
            if v == '+' or v == '-':
                self.adv()
                e = ('binop', v, e, self.parse_mul())
            else:
                return e

    def parse_mul(self):
        e = self.parse_unary()
        while True:
            v = self.t[self.i].val
            if v == '*' or v == '/' or v == '%':
                self.adv()
                e = ('binop', v, e, self.parse_unary())
            else:
                return e

    def parse_unary(self):
        v = self.t[self.i].val
        if v in ('!', '-', '+', '~', '*', '&'):
            self.adv()
            return ('unop', v, self.parse_unary())
        if v == '++' or v == '--':
            self.adv()
            return ('preinc', v, self.parse_unary())
        if v == 'sizeof':
            self.adv()
            if self.at('(') and self.val(1) in TYPE_WORDS:
                self.expect('(')
                ty = self.parse_type()
                self.expect(')')
                return ('sizeof_type', ty)
            return ('sizeof', self.parse_unary())
        return self.parse_postfix()

    def parse_postfix(self):
        e = self.parse_primary()
        while True:
            v = self.t[self.i].val
            if v == '(':
                args = self.parse_args()
                e = ('call', e[1], args) if e[0] == 'id' else ('callptr', e, args)
            elif v == '[':
                self.adv()
                idx = self.parse_expr()
                self.expect(']')
                e = ('index', e, idx)
            elif v == '.':
                self.adv()
                e = ('member', e, self.expect_id())
            elif v == '->':
                self.adv()
                e = ('ptrmember', e, self.expect_id())
            elif v == '++' or v == '--':
                self.adv()
                e = ('postinc', v, e)
            else:
                return e

    def parse_args(self):
        self.expect('(')
        args = []
        if not self.at(')'):
            while True:
                args.append(self.parse_assign())
                if not self.eat(','):
                    break
        self.expect(')')
        return args

    def parse_primary(self):
        t = self.t[self.i]
        k = t.kind
        if k == 'num':
            self.adv(); return ('num', t.val)
        if k == 'string':
            self.adv(); return ('str', t.val)
        if k == 'charlit':
            self.adv(); return ('char', t.val)
        v = t.val
        if v == 'true':
            self.adv(); return ('bool', 1)
        if v == 'false':
            self.adv(); return ('bool', 0)
        if v == 'nullptr':
            self.adv(); return ('nullptr',)
        if v == 'std' and self.val(1) == '::' and self.kind(2) == 'id':
            self.adv(); self.adv()
            return ('id', self.adv().val)
        if k == 'id':
            self.adv(); return ('id', v)
        if v == '(':
            self.adv()
            e = self.parse_expr()
            self.expect(')')
            return ('paren', e)
        raise SyntaxError(f'line {t.line}: unexpected token {v!r}')

    def infer(self, n):
        k = n[0]
        if k == 'num':
            s = n[1].lower().rstrip('ful')
            return 'double' if ('.' in s or 'e' in s) else 'int'
        if k == 'str': return 'char*'
        if k == 'char': return 'char'
        if k == 'bool': return 'bool'
        if k == 'nullptr': return 'void*'
        if k == 'id': return self.lookup(n[1]) or 'int'
        if k == 'paren': return self.infer(n[1])
        if k == 'unop':
            if n[1] == '&': return self.infer(n[2]) + '*'
            if n[1] == '*':
                t = self.infer(n[2])
                return t[:-1] if t.endswith('*') else 'int'
            return 'bool' if n[1] == '!' else self.infer(n[2])
        if k == 'preinc' or k == 'postinc':
            return self.infer(n[2])
        if k == 'index':
            t = self.infer(n[1])
            return t[:-1] if t.endswith('*') else 'int'
        if k == 'cond':
            a, b = self.infer(n[2]), self.infer(n[3])
            return a if a == b else ('double' if 'double' in (a, b) else 'int')
        if k == 'binop':
            if n[1] in ('==', '!=', '<', '>', '<=', '>=', '&&', '||'):
                return 'bool'
            a, b = self.infer(n[2]), self.infer(n[3])
            if '*' in a: return a
            if '*' in b: return b
            return 'double' if 'double' in (a, b) else 'int'
        if k == 'assign': return self.infer(n[2])
        if k == 'call':
            name = n[1]
            f = self.funcs.get(name)
            if f: return f['ret']
            if name in ('printf', 'scanf', 'puts', 'putchar', 'getchar'): return 'int'
            if name in ('fabs', 'sqrt', 'pow', 'sin', 'cos', 'tan', 'floor',
                        'ceil', 'exp', 'log', 'log10', 'round'):
                return 'double'
            return 'int'
        if k == 'callptr': return 'int'
        if k == 'sizeof' or k == 'sizeof_type': return 'int'
        if k == 'member' or k == 'ptrmember': return 'int'
        if k == 'comma': return self.infer(n[2])
        return 'int'

PRELUDE = r'''
#include <stdio.h>

static void mcpp_print_int(long long v) { printf("%lld", v); }
static void mcpp_print_uint(unsigned long long v) { printf("%llu", v); }
static void mcpp_print_double(double v) { printf("%g", v); }
static void mcpp_print_char(char v) { putchar((unsigned char)v); }
static void mcpp_print_cstr(const char *v) { fputs(v ? v : "(null)", stdout); }
static void mcpp_print_bool(int v) { fputs(v ? "true" : "false", stdout); }
static void mcpp_flush(void) { fflush(stdout); }
'''

class Emitter:
    __slots__ = ('p', 'items', 'scopes', '_ct_cache', '_pad_cache')

    CT = {
        'void': 'void', 'bool': 'int', 'char': 'char',
        'signed char': 'signed char', 'unsigned char': 'unsigned char',
        'short': 'short', 'signed short': 'short', 'unsigned short': 'unsigned short',
        'int': 'int', 'signed int': 'int', 'unsigned int': 'unsigned int',
        'long': 'long', 'signed long': 'long', 'unsigned long': 'unsigned long',
        'long long': 'long long', 'signed long long': 'long long',
        'unsigned long long': 'unsigned long long',
        'float': 'float', 'double': 'double',
        'const void': 'const void', 'const char': 'const char',
        'const short': 'const short', 'const int': 'const int',
        'const long': 'const long', 'const long long': 'const long long',
        'const float': 'const float', 'const double': 'const double',
    }

    def __init__(self, parser, items):
        self.p = parser
        self.items = items
        self.scopes = [dict(parser.globals)]
        self._ct_cache = {}
        self._pad_cache = {}

    def _pad(self, n):
        p = self._pad_cache.get(n)
        if p is None:
            p = self._pad_cache[n] = '    ' * n
        return p

    def ct(self, t):
        c = self._ct_cache.get(t)
        if c is not None:
            return c
        if t.endswith('[]'):
            c = self.ct(t[:-2]) + '*'
        elif t.endswith('&'):
            c = self.ct(t[:-1]) + '*'
        elif t.startswith('const ') and t not in self.CT:
            c = 'const ' + self.ct(t[6:])
        elif t.endswith('*'):
            c = self.ct(t[:-1]) + ' *'
        else:
            c = self.CT.get(t, 'int')
        self._ct_cache[t] = c
        return c

    def lookup(self, name):
        for s in reversed(self.scopes):
            if name in s:
                return s[name]
        return self.p.lookup(name)

    def expr(self, n):
        k = n[0]
        if k == 'num':
            s = n[1]
            return s[:-1] + 'f' if s.lower().endswith('f') else s
        if k == 'str': return n[1]
        if k == 'char': return n[1]
        if k == 'bool': return '1' if n[1] else '0'
        if k == 'nullptr': return 'NULL'
        if k == 'id': return n[1]
        if k == 'paren': return '(' + self.expr(n[1]) + ')'
        if k == 'unop': return '(' + n[1] + self.expr(n[2]) + ')'
        if k == 'preinc': return '(' + n[1] + self.expr(n[2]) + ')'
        if k == 'postinc': return '(' + self.expr(n[2]) + n[1] + ')'
        if k == 'binop': return '(' + self.expr(n[2]) + ' ' + n[1] + ' ' + self.expr(n[3]) + ')'
        if k == 'assign': return '(' + self.expr(n[2]) + ' ' + n[1] + ' ' + self.expr(n[3]) + ')'
        if k == 'cond': return '(' + self.expr(n[1]) + ' ? ' + self.expr(n[2]) + ' : ' + self.expr(n[3]) + ')'
        if k == 'comma': return '(' + self.expr(n[1]) + ', ' + self.expr(n[2]) + ')'
        if k == 'index': return '(' + self.expr(n[1]) + '[' + self.expr(n[2]) + '])'
        if k == 'member': return self.expr(n[1]) + '.' + n[2]
        if k == 'ptrmember': return self.expr(n[1]) + '->' + n[2]
        if k == 'call':
            a = n[2]
            return n[1] + '(' + (', '.join(self.expr(x) for x in a) if a else '') + ')'
        if k == 'callptr':
            a = n[2]
            return '(' + self.expr(n[1]) + ')(' + (', '.join(self.expr(x) for x in a) if a else '') + ')'
        if k == 'sizeof': return 'sizeof(' + self.expr(n[1]) + ')'
        if k == 'sizeof_type': return 'sizeof(' + self.ct(n[1]) + ')'
        raise SyntaxError(f'cannot emit expression node {k!r}')

    def init_value(self, init):
        if init is None:
            return None
        if init[0] == 'initlist':
            return '{' + ', '.join(self.init_value(v) for v in init[1]) + '}'
        return self.expr(init)

    def print_expr(self, n):
        t = self.p.infer(n)
        c = self.expr(n)
        if t == 'double' or t == 'float':
            return f'mcpp_print_double((double)({c}))'
        if t == 'char' or t == 'signed char' or t == 'unsigned char':
            return f'mcpp_print_char({c})'
        if t == 'bool':
            return f'mcpp_print_bool({c})'
        if '*' in t:
            return f'mcpp_print_cstr((const char*)({c}))'
        if 'unsigned' in t:
            return f'mcpp_print_uint((unsigned long long)({c}))'
        return f'mcpp_print_int((long long)({c}))'

    def emit_stmt(self, n, ind=0):
        pad = self._pad(ind)
        k = n[0]

        if k == 'block':
            self.scopes.append({})
            body = n[1]
            if not body:
                s = pad + '{}'
            else:
                inner = '\n'.join(self.emit_stmt(x, ind + 1) for x in body)
                s = pad + '{\n' + inner + '\n' + pad + '}'
            self.scopes.pop()
            return s

        if k == 'empty':
            return pad + ';'

        if k == 'decl':
            _, base_ty, items = n
            lines = []
            append = lines.append
            scope = self.scopes[-1]
            for name, init, actual, dims, storage in items:
                scope[name] = actual
                prefix = (' '.join(storage) + ' ') if storage else ''
                if dims:
                    ds = ''.join('[' + (self.expr(d) if d else '') + ']' for d in dims)
                    cty = self.ct(actual[:-2] if actual.endswith('[]') else actual)
                    iv = self.init_value(init)
                    if iv is not None:
                        append(f'{pad}{prefix}{cty} {name}{ds} = {iv};')
                    else:
                        append(f'{pad}{prefix}{cty} {name}{ds};')
                else:
                    iv = self.init_value(init)
                    cty = self.ct(actual)
                    if iv is not None:
                        append(f'{pad}{prefix}{cty} {name} = {iv};')
                    else:
                        append(f'{pad}{prefix}{cty} {name};')
            return '\n'.join(lines)

        if k == 'expr':
            return pad + self.expr(n[1]) + ';'

        if k == 'cout':
            lines = []
            append = lines.append
            for x in n[1]:
                if x[0] == 'endl':
                    append(pad + 'mcpp_print_char(\'\\n\');')
                    append(pad + 'mcpp_flush();')
                else:
                    append(pad + self.print_expr(x) + ';')
            return '\n'.join(lines) if lines else pad + ';'

        if k == 'cin':
            lines = []
            append = lines.append
            for x in n[1]:
                t = self.p.infer(x)
                c = self.expr(x)
                if t == 'double':
                    fmt = '"%lf"'
                elif t == 'float':
                    fmt = '"%f"'
                elif t == 'char':
                    fmt = '" %c"'
                elif '*' in t:
                    raise SyntaxError('cannot directly read into pointer with cin')
                else:
                    fmt = '"%d"'
                append(f'{pad}scanf({fmt}, &({c}));')
            return '\n'.join(lines) if lines else pad + ';'

        if k == 'if':
            s = pad + 'if (' + self.expr(n[1]) + ') ' + self.body(n[2], ind)
            if n[3] is not None:
                if n[3][0] == 'if':
                    s += '\n' + pad + 'else ' + self.emit_stmt(n[3], ind).lstrip()
                else:
                    s += '\n' + pad + 'else ' + self.body(n[3], ind)
            return s

        if k == 'while':
            return pad + 'while (' + self.expr(n[1]) + ') ' + self.body(n[2], ind)

        if k == 'do':
            return pad + 'do ' + self.body(n[1], ind) + ' while (' + self.expr(n[2]) + ');'

        if k == 'for':
            self.scopes.append({})
            init = self.emit_for_init(n[1])
            cond = self.expr(n[2]) if n[2] is not None else ''
            step = self.expr(n[3]) if n[3] is not None else ''
            b = self.body(n[4], ind)
            self.scopes.pop()
            return pad + f'for ({init}; {cond}; {step}) ' + b

        if k == 'switch':
            parts = [pad + 'switch (' + self.expr(n[1]) + ') {']
            inner_pad = self._pad(ind + 1)
            stmt_pad = ind + 2
            for kind2, value, stmts in n[2]:
                if kind2 == 'case':
                    parts.append(inner_pad + 'case ' + self.expr(value) + ':')
                else:
                    parts.append(inner_pad + 'default:')
                if stmts:
                    parts.extend(self.emit_stmt(s, stmt_pad) for s in stmts)
            parts.append(pad + '}')
            return '\n'.join(parts)

        if k == 'return':
            return pad + 'return' + ((' ' + self.expr(n[1])) if n[1] else '') + ';'
        if k == 'break':
            return pad + 'break;'
        if k == 'continue':
            return pad + 'continue;'
        raise SyntaxError(f'cannot emit statement node {k!r}')

    def emit_for_init(self, n):
        if n is None:
            return ''
        if n[0] == 'decl':
            _, base_ty, items = n
            scope = self.scopes[-1]
            vals = []
            for name, init, actual, dims, storage in items:
                scope[name] = actual
                if dims:
                    raise SyntaxError('array declarations in for-init are not supported')
                prefix = (' '.join(storage) + ' ') if storage else ''
                v = prefix + self.ct(actual) + ' ' + name
                if init is not None:
                    v += ' = ' + self.expr(init)
                vals.append(v)
            return ', '.join(vals)
        return self.expr(n[1])

    def body(self, stmt, ind):
        if stmt[0] == 'block':
            return self.emit_stmt(stmt, ind)
        return '{\n' + self.emit_stmt(stmt, ind + 1) + '\n' + self._pad(ind) + '}'

    def enum_def(self, item):
        _, name, vals, scoped = item
        out = [f'enum {name} {{' if name else 'enum {']
        n = len(vals)
        for i, (nm, ex) in enumerate(vals):
            out.append('    ' + nm + ' = ' + self.expr(ex) + (',' if i + 1 < n else ''))
        out.append('};')
        return '\n'.join(out)

    def func_def(self, item):
        _, ty, name, params, body, storage, defined = item
        pdecl = []
        for pt, pn, pst in params:
            prefix = (' '.join(pst) + ' ') if pst else ''
            pdecl.append(prefix + self.ct(pt) + ' ' + pn)
        ps = ', '.join(pdecl) or 'void'
        if not defined:
            return f'{self.ct(ty)} {name}({ps});'
        self.scopes.append({pn: pt for pt, pn, _ in params})
        b = self.emit_stmt(body, 0)
        self.scopes.pop()
        prefix = (' '.join(storage) + ' ') if storage else ''
        return f'{prefix}{self.ct(ty)} {name}({ps})\n{b}'

    def emit(self):
        out = ['/* ---- generated by miniC++ full-core ---- */', PRELUDE]

        for it in self.items:
            if it[0] == 'enum':
                out.append(self.enum_def(it))

        for it in self.items:
            if it[0] == 'func':
                _, ty, name, params, _body, storage, _defined = it
                pdecl = ', '.join(((' '.join(ps) + ' ') if ps else '') + self.ct(pt) + ' ' + pn
                                  for pt, pn, ps in params) or 'void'
                prefix = (' '.join(storage) + ' ') if storage else ''
                out.append(f'{prefix}{self.ct(ty)} {name}({pdecl});')

        for it in self.items:
            if it[0] != 'gvar':
                continue
            _, ty, name, dims, init, storage = it
            prefix = (' '.join(storage) + ' ') if storage else ''
            cty = self.ct(ty)
            if dims:
                ds = ''.join('[' + (self.expr(d) if d else '') + ']' for d in dims)
                iv = self.init_value(init)
                out.append(f'{prefix}{cty} {name}{ds}' + (f' = {iv}' if iv is not None else '') + ';')
            else:
                iv = self.init_value(init)
                out.append(f'{prefix}{cty} {name}' + (f' = {iv}' if iv is not None else '') + ';')

        for it in self.items:
            if it[0] == 'func' and it[6]:
                out.append('')
                out.append(self.func_def(it))

        return '\n'.join(out) + '\n'

USAGE = ('usage: minicpp_full.py FILE.cpp [-o OUT.c] [--run] [--cc COMPILER]\n\n'
         '  no -o       print generated C\n'
         '  -o FILE     write generated C to FILE\n'
         '  --run       compile generated C with a C compiler and run it\n'
         '  --cc NAME   choose the C compiler (default: cc)\n')

def compile_source(src):
    clean = preprocess(src)
    toks = tokenize(clean)
    p = Parser(toks)
    items = p.parse_program()
    return Emitter(p, items).emit()

def main(argv):
    if len(argv) < 2 or argv[1] in ('-h', '--help'):
        sys.stdout.write(USAGE)
        return 0

    src_path = argv[1]
    out_path = None
    run = False
    cc = os.environ.get('CC', 'cc')

    i = 2
    while i < len(argv):
        a = argv[i]
        if a == '-o':
            i += 1
            if i >= len(argv):
                sys.stderr.write('error: -o needs an argument\n')
                return 1
            out_path = argv[i]
        elif a == '--run':
            run = True
        elif a == '--cc':
            i += 1
            if i >= len(argv):
                sys.stderr.write('error: --cc needs an argument\n')
                return 1
            cc = argv[i]
        else:
            sys.stderr.write(f'error: unknown option {a!r}\n')
            return 1
        i += 1

    try:
        with open(src_path, 'r', encoding='utf-8') as f:
            src = f.read()
    except OSError as e:
        sys.stderr.write(f'error: {e}\n')
        return 1

    try:
        code = compile_source(src)
    except SyntaxError as e:
        sys.stderr.write(f'miniC++ error: {e}\n')
        return 1

    if out_path:
        try:
            with open(out_path, 'w', encoding='utf-8') as f:
                f.write(code)
        except OSError as e:
            sys.stderr.write(f'error: {e}\n')
            return 1
        sys.stderr.write(f'wrote {out_path}\n')

    if run:
        with tempfile.TemporaryDirectory(prefix='minicpp_') as td:
            cfile = os.path.join(td, 'out.c')
            exe = os.path.join(td, 'out')
            with open(cfile, 'w', encoding='utf-8') as f:
                f.write(code)
            r = subprocess.run([cc, '-std=c11', '-O2', '-w', '-o', exe, cfile], check=False)
            if r.returncode:
                sys.stderr.write('C compilation failed\n')
                return r.returncode
            return subprocess.run([exe], check=False).returncode

    if not out_path:
        sys.stdout.write(code)
    return 0

if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
