# G3Plugins - Arquitetura do Projeto

Versão: 1.0

---

# Objetivo

O projeto G3Plugins tem como objetivo desenvolver um conjunto de plugins profissionais para Autodesk Revit voltados para aumentar a produtividade em projetos estruturais.

O primeiro módulo do projeto será um sistema inteligente de cotagem automática de paredes.

Todo o código deverá ser desenvolvido pensando em:

- reutilização
- organização
- performance
- manutenção
- escalabilidade

O projeto deverá permanecer organizado mesmo após anos de desenvolvimento.

Nunca implementar soluções rápidas que prejudiquem a arquitetura.

---

# Filosofia do projeto

A arquitetura tem prioridade sobre velocidade.

É preferível gastar mais tempo criando uma solução reutilizável do que escrever código rápido que precisará ser refeito.

Sempre pensar:

"Esse código poderá ser utilizado novamente?"

Se a resposta for sim, ele deve virar um Service.

---

# Estrutura do projeto

Application/

Inicialização do plugin.

Responsável por registrar o Ribbon.

Nunca implementar lógica de negócio.

---

Ribbon/

Responsável apenas pela interface do Revit.

Criação de abas.

Criação de painéis.

Criação de botões.

Nunca executar regras de negócio.

---

Commands/

Cada botão do Ribbon possui um Command.

Commands apenas iniciam a execução.

Commands nunca implementam algoritmos.

Commands chamam Services.

---

GeometryServices/

Responsável por toda leitura da geometria do Revit.

Exemplos:

- faces
- arestas
- sólidos
- interseções
- aberturas
- referências

Nenhuma cota é criada aqui.

---

DimensionServices/

Responsável exclusivamente pela criação das cotas.

Nunca localizar geometria.

Recebe referências prontas.

---

Models/

Classes responsáveis apenas por armazenar informações.

Models nunca executam lógica.

---

Utils/

Funções auxiliares.

Conversões.

Métodos estáticos.

Validações.

---

Resources/

Ícones.

Imagens.

Arquivos auxiliares.

---

UI/

Janelas WPF.

Configurações.

Nunca acessar diretamente a API do Revit.

---

# Responsabilidade única

Cada classe possui apenas uma responsabilidade.

Exemplo correto

WallAnalyzer

↓

Encontra informações da parede.

Não cria cotas.

Não cria interface.

Não altera documentos.

---

DimensionCreator

↓

Recebe referências.

Cria cotas.

Nada mais.

---

# Fluxo de execução

Usuário

↓

Ribbon

↓

Command

↓

GeometryServices

↓

DimensionServices

↓

Resultado

---

# Desenvolvimento incremental

Nunca desenvolver uma funcionalidade inteira de uma vez.

Sempre dividir em pequenas etapas.

Exemplo:

Selecionar parede

↓

Obter geometria

↓

Encontrar faces

↓

Encontrar portas

↓

Encontrar janelas

↓

Encontrar interseções

↓

Ordenar referências

↓

Criar linha de cota

↓

Criar cota

Cada etapa deve funcionar antes da próxima começar.

---

# Convenções

Classes:

PascalCase

Métodos:

PascalCase

Variáveis:

camelCase

Nunca utilizar abreviações desnecessárias.

Ruim:

GetDim()

Bom:

CreateDimension()

Ruim:

WA

Bom:

WallAnalyzer

---

# Comentários

Comentários devem explicar decisões.

Nunca explicar código óbvio.

Ruim:

// Soma 1

i++

Bom:

// Necessário para manter a ordem das referências conforme a direção da parede.

---

# Performance

Nunca percorrer o documento inteiro sem necessidade.

Sempre utilizar filtros.

Evitar criar FilteredElementCollector repetidamente.

Evitar cálculos duplicados.

Reutilizar resultados sempre que possível.

---

# Tratamento de erros

Nunca utilizar try/catch vazios.

Toda exceção deve possuir tratamento.

Mensagens para o usuário devem ser claras.

---

# Transações

Criar Transaction apenas quando realmente modificar o documento.

Nunca abrir Transaction durante leitura de geometria.

---

# Código duplicado

Código duplicado é proibido.

Se um trecho puder ser reutilizado por dois Commands, transformá-lo em Service.

---

# Escalabilidade

Toda nova funcionalidade deve ser adicionada sem modificar funcionalidades existentes.

Preferir adicionar novas classes do que aumentar classes já grandes.

---

# Regras para Inteligência Artificial

Qualquer IA utilizada neste projeto deverá seguir obrigatoriamente estas regras.

Nunca alterar a arquitetura.

Nunca criar classes sem necessidade.

Nunca mover arquivos sem autorização.

Nunca alterar namespaces sem autorização.

Nunca alterar o Ribbon automaticamente.

Nunca modificar código existente apenas por preferência.

Sempre reutilizar Services existentes.

Nunca criar código duplicado.

Toda nova funcionalidade deverá respeitar a responsabilidade de cada pasta.

Caso exista dúvida sobre onde implementar uma funcionalidade, perguntar antes de gerar código.

Antes de criar uma nova classe, verificar se já existe uma classe com responsabilidade semelhante.

Sempre gerar código limpo.

Sempre seguir os princípios SOLID.

Nunca utilizar soluções temporárias ("gambiarras").

---

# Objetivo final

O objetivo é construir um plugin profissional para Revit que possa crescer durante anos mantendo a mesma organização, qualidade e facilidade de manutenção.