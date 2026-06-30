TEX = moe_lpt_te_scheme.tex
PDF = moe_lpt_te_scheme.pdf

.PHONY: all clean

all: $(PDF)

$(PDF): $(TEX)
	latexmk -xelatex -interaction=nonstopmode -halt-on-error $(TEX)

clean:
	latexmk -C $(TEX)
