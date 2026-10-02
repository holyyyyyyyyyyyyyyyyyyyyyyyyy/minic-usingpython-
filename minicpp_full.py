#!/usr/bin/env python3
import os
import re
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, font as tkfont

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
            if m.group(1) not in ALLOWED_INCLUDES:
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
                raise SyntaxError(f'line {self.t[self.i].line}: statement before case/default')
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
                    append(pad + "mcpp_print_char('\\n');")
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
        out = ['/* generated by miniC++ full-core */', PRELUDE]

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

def compile_source(src):
    clean = preprocess(src)
    toks = tokenize(clean)
    p = Parser(toks)
    items = p.parse_program()
    return Emitter(p, items).emit()

def find_c_compiler():
    env = os.environ.get('CC')
    for cand in (env, 'cc', 'gcc', 'clang', 'tcc'):
        if not cand:
            continue
        try:
            r = subprocess.run([cand, '--version'], capture_output=True, timeout=5)
            if r.returncode == 0:
                return cand
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
    return None

BG        = '#1e1e1e'
BG2       = '#252526'
BAR       = '#2d2d30'
BTN       = '#3e3e42'
BTN_HOVER = '#505055'
FG        = '#d4d4d4'
FG_DIM    = '#858585'
ACCENT    = '#007acc'
ERR       = '#f48771'
OK        = '#89d185'

EXAMPLE = '''#include <iostream>
#include <stdio.h>
using namespace std;

int main() {
    cout << "Hello from miniC++!" << endl;

    cout << "What is your name? ";
    char name[64];
    scanf("%63s", name);
    printf("Nice to meet you, %s!\\n", name);

    cout << "Counting from 1 to 5:" << endl;
    for (int i = 1; i <= 5; i++) {
        cout << i << " ";
    }
    cout << endl;

    int a = 10, b = 3;
    cout << a << " + " << b << " = " << a + b << endl;
    cout << a << " / " << b << " = " << a / b << endl;

    if (a > b) {
        cout << "a is larger than b" << endl;
    } else {
        cout << "b is larger" << endl;
    }

    return 0;
}
'''

class IDE:
    def __init__(self, root):
        self.root = root
        self.cc = find_c_compiler()
        self.running = False

        root.title('miniC++ IDE')
        root.geometry('1200x780')
        root.minsize(800, 500)
        root.configure(bg=BG)

        mono = tkfont.Font(family='Consolas', size=11)
        if not mono.actual('family') == 'Consolas':
            for name in ('Menlo', 'DejaVu Sans Mono', 'Courier New', 'monospace'):
                f = tkfont.Font(family=name, size=11)
                if f.actual('family') == name or name == 'monospace':
                    mono = f
                    break
        self.mono = mono
        self.uifont = tkfont.Font(family='Segoe UI', size=9)

        self._build_toolbar()
        self._build_main()
        self._build_status()
        self._bind_keys()

        self.editor.insert('1.0', EXAMPLE)
        self._reset_modified()

    def _build_toolbar(self):
        bar = tk.Frame(self.root, bg=BAR, height=38)
        bar.pack(side='top', fill='x')
        bar.pack_propagate(False)

        def make_btn(text, cmd, width=10):
            b = tk.Button(bar, text=text, command=cmd, bg=BTN, fg=FG,
                          activebackground=BTN_HOVER, activeforeground='#ffffff',
                          relief='flat', bd=0, padx=12, pady=4,
                          font=self.uifont, cursor='hand2')
            b.pack(side='left', padx=3, pady=5)
            b.bind('<Enter>', lambda e, btn=b: btn.config(bg=BTN_HOVER))
            b.bind('<Leave>', lambda e, btn=b: btn.config(bg=BTN))
            return b

        self.run_btn = make_btn('▶  Run  (F5)', self.run_code)
        self.run_btn.config(bg='#0e7a0d', activebackground='#129712')
        self.run_btn.bind('<Enter>', lambda e: self.run_btn.config(bg='#129712'))
        self.run_btn.bind('<Leave>', lambda e: self.run_btn.config(bg='#0e7a0d'))

        make_btn('Clear Code', self.clear_code)
        make_btn('Clear Output', self.clear_output)
        make_btn('Clear Input', self.clear_input)
        make_btn('Load…', self.load_file)
        make_btn('Save…', self.save_file)
        make_btn('Show C', self.show_c)

        right = tk.Frame(bar, bg=BAR)
        right.pack(side='right', padx=8)
        cc_text = self.cc if self.cc else 'NOT FOUND'
        cc_color = FG_DIM if self.cc else ERR
        tk.Label(right, text=f'C compiler: {cc_text}', bg=BAR, fg=cc_color,
                 font=self.uifont).pack(side='right', pady=8)

    def _build_main(self):
        main = tk.PanedWindow(self.root, orient='horizontal', bg=BG,
                              sashwidth=4, bd=0, sashrelief='flat')
        main.pack(fill='both', expand=True)

        left = tk.Frame(main, bg=BG)
        main.add(left, minsize=380, stretch='always', width=700)

        self._section_header(left, 'Source Code')
        code_wrap = tk.Frame(left, bg=BG)
        code_wrap.pack(fill='both', expand=True)
        self.editor = tk.Text(code_wrap, bg=BG, fg=FG, insertbackground='#aeafad',
                              selectbackground='#264f78', selectforeground='#ffffff',
                              font=self.mono, wrap='none', undo=True, bd=0,
                              padx=10, pady=8, highlightthickness=0)
        self.editor.pack(side='left', fill='both', expand=True)
        vsb = tk.Scrollbar(code_wrap, command=self.editor.yview, bg=BG2,
                           troughcolor=BG, bd=0, width=12)
        vsb.pack(side='right', fill='y')
        self.editor.config(yscrollcommand=vsb.set)

        right = tk.PanedWindow(main, orient='vertical', bg=BG,
                               sashwidth=4, bd=0, sashrelief='flat')
        main.add(right, minsize=300, stretch='always')

        inp = tk.Frame(right, bg=BG)
        right.add(inp, minsize=90, stretch='never', height=140)
        self._section_header(inp, 'Standard Input  (stdin)')
        self.stdin_box = tk.Text(inp, bg=BG, fg=FG, insertbackground='#aeafad',
                                 selectbackground='#264f78', font=self.mono,
                                 wrap='none', bd=0, padx=10, pady=6,
                                 height=5, highlightthickness=0)
        self.stdin_box.pack(fill='both', expand=True)

        outp = tk.Frame(right, bg=BG)
        right.add(outp, minsize=180, stretch='always')
        hdr = tk.Frame(outp, bg=BG2, height=24)
        hdr.pack(fill='x')
        hdr.pack_propagate(False)
        tk.Label(hdr, text='Output', bg=BG2, fg=FG_DIM, anchor='w',
                 padx=10, font=self.uifont).pack(side='left')
        self.out_status = tk.Label(hdr, text='', bg=BG2, fg=FG_DIM,
                                   anchor='e', padx=10, font=self.uifont)
        self.out_status.pack(side='right')

        out_wrap = tk.Frame(outp, bg=BG)
        out_wrap.pack(fill='both', expand=True)
        self.output = tk.Text(out_wrap, bg=BG, fg=FG, font=self.mono,
                              wrap='word', bd=0, padx=10, pady=8,
                              state='disabled', highlightthickness=0)
        self.output.pack(side='left', fill='both', expand=True)
        osb = tk.Scrollbar(out_wrap, command=self.output.yview, bg=BG2,
                           troughcolor=BG, bd=0, width=12)
        osb.pack(side='right', fill='y')
        self.output.config(yscrollcommand=osb.set)
        self.output.tag_config('error', foreground=ERR)
        self.output.tag_config('dim', foreground=FG_DIM)
        self.output.tag_config('ok', foreground=OK)

    def _section_header(self, parent, text):
        h = tk.Frame(parent, bg=BG2, height=24)
        h.pack(fill='x')
        h.pack_propagate(False)
        tk.Label(h, text=text, bg=BG2, fg=FG_DIM, anchor='w',
                 padx=10, font=self.uifont).pack(side='left')

    def _build_status(self):
        bar = tk.Frame(self.root, bg=BAR, height=22)
        bar.pack(side='bottom', fill='x')
        bar.pack_propagate(False)
        self.status = tk.Label(bar, text='Ready', bg=BAR, fg=FG_DIM,
                               anchor='w', padx=10, font=self.uifont)
        self.status.pack(side='left')
        self.pos_lbl = tk.Label(bar, text='Ln 1, Col 1', bg=BAR, fg=FG_DIM,
                                anchor='e', padx=10, font=self.uifont)
        self.pos_lbl.pack(side='right')
        self.editor.bind('<KeyRelease>', self._update_pos)
        self.editor.bind('<ButtonRelease>', self._update_pos)

    def _update_pos(self, _ev=None):
        idx = self.editor.index('insert')
        ln, col = idx.split('.')
        self.pos_lbl.config(text=f'Ln {ln}, Col {int(col) + 1}')

    def _bind_keys(self):
        self.root.bind('<F5>', lambda e: self.run_code())
        self.root.bind('<Control-Return>', lambda e: self.run_code())
        self.root.bind('<Control-l>', lambda e: self.clear_output())
        self.root.bind('<Control-o>', lambda e: self.load_file())
        self.root.bind('<Control-s>', lambda e: self.save_file())

    def _set_status(self, text, color=FG_DIM):
        self.status.config(text=text, fg=color)

    def _reset_modified(self):
        self.editor.edit_modified(False)

    def _append_out(self, text, tag=None):
        self.output.config(state='normal')
        if tag:
            self.output.insert('end', text, tag)
        else:
            self.output.insert('end', text)
        self.output.see('end')
        self.output.config(state='disabled')

    def _append_out_safe(self, text, tag=None):
        self.root.after(0, lambda: self._append_out(text, tag))

    def _set_out_status(self, text, color=FG_DIM):
        self.root.after(0, lambda: self.out_status.config(text=text, fg=color))

    def _set_running(self, on):
        def do():
            self.running = on
            if on:
                self.run_btn.config(state='disabled', text='⏳  Running…')
                self._set_status('Compiling and running…', FG_DIM)
            else:
                self.run_btn.config(state='normal', text='▶  Run  (F5)')
        self.root.after(0, do)

    def clear_code(self):
        self.editor.delete('1.0', 'end')

    def clear_output(self):
        self.output.config(state='normal')
        self.output.delete('1.0', 'end')
        self.output.config(state='disabled')
        self._set_out_status('')

    def clear_input(self):
        self.stdin_box.delete('1.0', 'end')

    def load_file(self):
        path = filedialog.askopenfilename(
            title='Open source file',
            filetypes=[('C++ source', '*.cpp *.cc *.cxx *.c'), ('All files', '*.*')])
        if not path:
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                self.editor.delete('1.0', 'end')
                self.editor.insert('1.0', f.read())
            self._set_status(f'Loaded {os.path.basename(path)}', FG_DIM)
        except OSError as e:
            messagebox.showerror('Load error', str(e))

    def save_file(self):
        path = filedialog.asksaveasfilename(
            title='Save source file', defaultextension='.cpp',
            filetypes=[('C++ source', '*.cpp'), ('All files', '*.*')])
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(self.editor.get('1.0', 'end-1c'))
            self._set_status(f'Saved {os.path.basename(path)}', FG_DIM)
        except OSError as e:
            messagebox.showerror('Save error', str(e))

    def show_c(self):
        code = self.editor.get('1.0', 'end-1c')
        try:
            c_code = compile_source(code)
        except SyntaxError as e:
            messagebox.showerror('Compile error', str(e))
            return
        top = tk.Toplevel(self.root)
        top.title('Generated C code')
        top.geometry('800x600')
        top.configure(bg=BG)
        t = tk.Text(top, bg=BG, fg=FG, font=self.mono, wrap='none',
                    bd=0, padx=10, pady=8, highlightthickness=0)
        t.pack(fill='both', expand=True)
        t.insert('1.0', c_code)
        t.config(state='disabled')

    def run_code(self):
        if self.running:
            return
        if not self.cc:
            messagebox.showerror(
                'No C compiler found',
                'miniC++ needs a C compiler (cc, gcc, clang, or tcc) to build the '
                'generated program.\n\nInstall one and make sure it is on your PATH, '
                'or set the CC environment variable.')
            return

        code = self.editor.get('1.0', 'end-1c')
        stdin_text = self.stdin_box.get('1.0', 'end-1c')
        self.clear_output()
        self._set_running(True)
        self._set_out_status('')

        threading.Thread(
            target=self._worker, args=(code, stdin_text),
            daemon=True).start()

    def _worker(self, code, stdin_text):
        try:
            try:
                c_code = compile_source(code)
            except SyntaxError as e:
                self._append_out_safe(f'miniC++ compile error: {e}\n', 'error')
                self._set_status('Compile failed', ERR)
                self._set_out_status('error', ERR)
                return

            with tempfile.TemporaryDirectory(prefix='minicpp_') as td:
                cfile = os.path.join(td, 'out.c')
                exe = os.path.join(td, 'out')
                if sys.platform.startswith('win'):
                    exe += '.exe'
                with open(cfile, 'w', encoding='utf-8') as f:
                    f.write(c_code)

                try:
                    r = subprocess.run(
                        [self.cc, '-std=c11', '-O2', '-w', '-o', exe, cfile],
                        capture_output=True, text=True, timeout=30)
                except subprocess.TimeoutExpired:
                    self._append_out_safe('C compilation timed out.\n', 'error')
                    self._set_status('C compile timeout', ERR)
                    self._set_out_status('error', ERR)
                    return

                if r.returncode != 0:
                    self._append_out_safe('C compilation failed:\n', 'error')
                    self._append_out_safe((r.stderr or '(no stderr)') + '\n', 'error')
                    self._set_status('C compile failed', ERR)
                    self._set_out_status('error', ERR)
                    return

                try:
                    r = subprocess.run(
                        [exe], input=stdin_text, capture_output=True,
                        text=True, timeout=15)
                except subprocess.TimeoutExpired:
                    self._append_out_safe('\nProgram timed out (15s).\n', 'error')
                    self._set_status('Program timeout', ERR)
                    self._set_out_status('timeout', ERR)
                    return

                if r.stdout:
                    self._append_out_safe(r.stdout)
                if r.stderr:
                    if r.stdout and not r.stdout.endswith('\n'):
                        self._append_out_safe('\n')
                    self._append_out_safe('[stderr]\n', 'dim')
                    self._append_out_safe(r.stderr, 'error')

                if not r.stdout and not r.stderr:
                    self._append_out_safe('(program produced no output)\n', 'dim')

                self._append_out_safe(f'\n[exit code: {r.returncode}]\n', 'dim')
                if r.returncode == 0:
                    self._set_status('Finished successfully', OK)
                    self._set_out_status('ok', OK)
                else:
                    self._set_status(f'Finished with code {r.returncode}', ERR)
                    self._set_out_status(f'exit {r.returncode}', ERR)
        except Exception as e:
            self._append_out_safe(f'Unexpected error: {e}\n', 'error')
            self._set_status('Error', ERR)
            self._set_out_status('error', ERR)
        finally:
            self._set_running(False)


def main():
    root = tk.Tk()
    IDE(root)
    root.mainloop()


if __name__ == '__main__':
    main()
