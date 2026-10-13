// expect: line 7: 'def' is not supported
pipeline {
    agent any
    stages {
        stage('A') {
            steps {
                def x = 1
                echo 'x'
            }
        }
    }
}
