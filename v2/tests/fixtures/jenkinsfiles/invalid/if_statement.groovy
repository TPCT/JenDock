// expect: line 7: Groovy 'if' is not supported
pipeline {
    agent any
    stages {
        stage('A') {
            steps {
                if (env.BRANCH_NAME == 'main') {
                    echo 'x'
                }
            }
        }
    }
}
