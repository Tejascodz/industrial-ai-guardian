CXX ?= g++
CXXFLAGS ?= -O2 -std=c++17 -w
SRC = te_cli.cpp te/teprob.cpp te/TEPlant.cpp te/TEController.cpp te/ControlLoops.cpp te/PI.cpp

te_cli: $(SRC)
	$(CXX) $(CXXFLAGS) -o $@ $(SRC)

clean:
	rm -f te_cli
